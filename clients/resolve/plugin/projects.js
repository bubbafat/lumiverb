// The plugin's Lumiverb projects. "New from Resolve project…": the Lumiverb
// clips a Resolve project's Media Pool holds, found by path; a snapshot when
// asked, nothing keeps the two in step afterwards. And "Also add to project"
// after an add in Resolve.

const fs = require('fs');
const { onDisk } = require('./paths');
const { currentProjectName, projectFilePaths } = require('./resolve');

// Lookups in flight at once.
const CONCURRENCY = 6;

// Library roots as the disk spells them (the brain's `/Volumes/media-01` is
// mounted as `/Volumes/MEDIA-01`), longest first so the deepest root wins,
// as the server's change reports match.
function rootsOnDisk(roots, realpath) {
    return [...roots]
        .map(([libraryId, root]) => ({ libraryId, root: onDisk(root, realpath) || root.normalize('NFC') }))
        .sort((a, b) => b.root.length - a.root.length);
}

// A file's library and path in it, or null when it's under no library.
function libraryPathOf(filePath, roots) {
    for (const { libraryId, root } of roots) {
        if (filePath.startsWith(`${root}/`)) return { libraryId, relPath: filePath.slice(root.length + 1) };
    }
    return null;
}

// `filePaths`: the Media Pool's files by pathKey (paths.js), in its order.
// Returns the Lumiverb clips among them in the same order, and the files
// Lumiverb doesn't have. `onProgress(done, total)` after each.
async function matchFiles({ client, filePaths, realpath = fs.realpathSync.native, onProgress = () => {} }) {
    const roots = rootsOnDisk(await client.libraryRoots(), realpath);
    const found = new Array(filePaths.length).fill(null);
    let next = 0;
    let done = 0;
    async function worker() {
        while (next < filePaths.length) {
            const index = next;
            next += 1;
            const where = libraryPathOf(filePaths[index], roots);
            found[index] = where ? await client.assetIdAt(where.libraryId, where.relPath) : null;
            done += 1;
            onProgress(done, filePaths.length);
        }
    }
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, filePaths.length) }, worker));
    return {
        assetIds: [...new Set(found.filter(Boolean))],
        notInLumiverb: filePaths.filter((_, index) => !found[index]),
    };
}

// Makes a Lumiverb project, named after the open Resolve project, of the
// Lumiverb clips its Media Pool holds. `onProgress({ step, done, total })`:
// step 'reading' (total unknown) then 'matching'.
async function newProjectFromResolve({ resolve, client, realpath = fs.realpathSync.native, onProgress = () => {} }) {
    const name = await currentProjectName(resolve);
    if (!name) throw new Error('Open a project in Resolve first.');
    const filePaths = [...(await projectFilePaths(resolve, realpath, (done) => onProgress({ step: 'reading', done, total: null })))];
    const { assetIds, notInLumiverb } = await matchFiles({
        client, filePaths, realpath, onProgress: (done, total) => onProgress({ step: 'matching', done, total }),
    });
    if (assetIds.length === 0) throw new Error(`None of the clips in ${name} are in Lumiverb.`);
    const project = await client.createProject(name, assetIds);
    return { project, clips: assetIds.length, notInLumiverb };
}

// After a clip is added in Resolve (`message` says how), adds it to the
// Lumiverb project too when one is given. What happened in Resolve stands
// whatever the project add does.
async function alsoAddToProject({ client, message, projectId, assetId }) {
    if (!projectId) return message;
    try {
        const added = await client.addToProject(projectId, [assetId]);
        return `${message} ${added ? 'Added to the project.' : 'Already in the project.'}`;
    } catch (error) {
        return `${message} Couldn't add it to the project: ${error.message}`;
    }
}

module.exports = { CONCURRENCY, alsoAddToProject, libraryPathOf, matchFiles, newProjectFromResolve, rootsOnDisk };
