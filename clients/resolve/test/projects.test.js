const assert = require('node:assert/strict');
const { test } = require('node:test');

const {
    CONCURRENCY,
    alsoAddToProject,
    libraryPathOf,
    matchFiles,
    newProjectFromResolve,
    rootsOnDisk,
} = require('../plugin/projects');

// The brain's roots say /Volumes/media-01; the disk is mounted as
// /Volumes/MEDIA-01 (what Resolve stores, and what realpath answers).
const disk = (p) => p.replace('/Volumes/media-01', '/Volumes/MEDIA-01');
const ROOTS = [['lib1', '/Volumes/media-01/Media'], ['lib2', '/Volumes/media-01/Media/Clients']];

// A Resolve project whose Media Pool's root bin holds these files (null: a
// timeline, which has no file).
function fakeResolve(name, filePaths) {
    const clip = (filePath) => ({ GetClipProperty: async () => ({ 'File Path': filePath || '' }) });
    const bin = { GetClipList: async () => filePaths.map(clip), GetSubFolderList: async () => [] };
    const project = { GetName: async () => name, GetMediaPool: async () => ({ GetRootFolder: async () => bin }) };
    return { GetProjectManager: async () => ({ GetCurrentProject: async () => project }) };
}

// A fake Lumiverb: `assets` maps "library|rel_path" to an asset id. Counts
// the lookups in flight at once and records the projects made.
function fakeLumiverb(assets) {
    const fake = {
        inFlight: 0,
        mostInFlight: 0,
        created: [],
        libraryRoots: async () => new Map(ROOTS),
        assetIdAt: async (libraryId, relPath) => {
            fake.inFlight += 1;
            fake.mostInFlight = Math.max(fake.mostInFlight, fake.inFlight);
            await new Promise((resolve) => setImmediate(resolve));
            fake.inFlight -= 1;
            return assets[`${libraryId}|${relPath}`] || null;
        },
        createProject: async (name, assetIds) => {
            fake.created.push({ name, assetIds });
            return { projectId: 'prj_new', name, own: true };
        },
    };
    return fake;
}

test('rootsOnDisk spells each root as the disk does, deepest first', () => {
    assert.deepEqual(rootsOnDisk(new Map(ROOTS), disk), [
        { libraryId: 'lib2', root: '/Volumes/MEDIA-01/Media/Clients' },
        { libraryId: 'lib1', root: '/Volumes/MEDIA-01/Media' },
    ]);
});

test('libraryPathOf finds the deepest library holding a file, by whole folders', () => {
    const roots = rootsOnDisk(new Map(ROOTS), disk);
    assert.deepEqual(libraryPathOf('/Volumes/MEDIA-01/Media/Clients/acme/a.mov', roots), { libraryId: 'lib2', relPath: 'acme/a.mov' });
    assert.deepEqual(libraryPathOf('/Volumes/MEDIA-01/Media/2026/b.mov', roots), { libraryId: 'lib1', relPath: '2026/b.mov' });
    assert.equal(libraryPathOf('/Volumes/MEDIA-01/MediaOld/c.mov', roots), null);
    assert.equal(libraryPathOf('/Users/robert/Music/track.wav', roots), null);
});

test("matchFiles keeps the Media Pool's order and lists what Lumiverb doesn't have", async () => {
    const files = Array.from({ length: 20 }, (_, i) => `/Volumes/MEDIA-01/Media/2026/clip${i}.mov`);
    const assets = Object.fromEntries(files.map((_, i) => [`lib1|2026/clip${i}.mov`, `a${i}`]));
    delete assets['lib1|2026/clip3.mov'];
    const client = fakeLumiverb(assets);
    const progress = [];

    const result = await matchFiles({ client, filePaths: files, realpath: disk, onProgress: (done, total) => progress.push([done, total]) });

    assert.deepEqual(result.assetIds, files.map((_, i) => `a${i}`).filter((id) => id !== 'a3'));
    assert.deepEqual(result.notInLumiverb, [files[3]]);
    assert.ok(client.mostInFlight <= CONCURRENCY, `${client.mostInFlight} lookups at once`);
    assert.ok(client.mostInFlight > 1, 'lookups overlap');
    assert.deepEqual(progress.at(-1), [20, 20]);
});

test('newProjectFromResolve makes a project named after the Resolve project, of its Lumiverb clips', async () => {
    const resolve = fakeResolve('NCL Jewel Walkthrough', [
        '/Volumes/MEDIA-01/Media/Cruise/a.mov',
        null,
        '/Volumes/MEDIA-01/Projects/music.wav',
        '/Volumes/MEDIA-01/Media/Cruise/b.mov',
    ]);
    const client = fakeLumiverb({ 'lib1|Cruise/a.mov': 'a1', 'lib1|Cruise/b.mov': 'a2' });
    const steps = [];

    const result = await newProjectFromResolve({ resolve, client, realpath: disk, onProgress: (p) => steps.push(p.step) });

    assert.deepEqual(client.created, [{ name: 'NCL Jewel Walkthrough', assetIds: ['a1', 'a2'] }]);
    assert.deepEqual(result, {
        project: { projectId: 'prj_new', name: 'NCL Jewel Walkthrough', own: true },
        clips: 2,
        notInLumiverb: ['/Volumes/MEDIA-01/Projects/music.wav'],
    });
    assert.deepEqual([...new Set(steps)], ['reading', 'matching']);
});

test('newProjectFromResolve makes nothing when none of the clips are in Lumiverb', async () => {
    const client = fakeLumiverb({});
    const resolve = fakeResolve('Empty', ['/Users/robert/Music/track.wav']);
    await assert.rejects(newProjectFromResolve({ resolve, client, realpath: disk }), /None of the clips in Empty are in Lumiverb\./);
    assert.deepEqual(client.created, []);
});

test('alsoAddToProject says what happened in the project after what happened in Resolve', async () => {
    const client = (outcome) => ({
        addToProject: async () => {
            if (outcome instanceof Error) throw outcome;
            return outcome;
        },
    });
    const args = { message: 'Added to Master.', projectId: 'prj_1', assetId: 'a1' };

    assert.equal(await alsoAddToProject({ ...args, client: client(1) }), 'Added to Master. Added to the project.');
    assert.equal(await alsoAddToProject({ ...args, client: client(0) }), 'Added to Master. Already in the project.');
    assert.equal(
        await alsoAddToProject({ ...args, client: client(new Error('Only the project owner can perform this action')) }),
        "Added to Master. Couldn't add it to the project: Only the project owner can perform this action",
    );
    assert.equal(await alsoAddToProject({ ...args, projectId: null, client: client(1) }), 'Added to Master.');
});
