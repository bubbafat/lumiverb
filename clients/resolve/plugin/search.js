// One page of results for the plugin: each clip with its path on this Mac,
// leaving out the ones already in the Resolve project when asked.
//
// Lumiverb can't filter by what's in Resolve, so hidden clips are made up
// for here: each further request asks for just the number still missing,
// so a page never runs past PAGE_SIZE and nothing is skipped (the cursor
// always ends right after the last clip looked at).

const fs = require('fs');
const path = require('path');
const { PAGE_SIZE, localPath } = require('./lumiverb');
const { onDisk } = require('./paths');

// Requests per page at most, for a search whose clips are nearly all in the
// project already.
const MAX_REQUESTS = 10;

// `localPath` is the file's real path when it's on this Mac: what's imported,
// and what's compared with the project's files.
function toClip(item, roots, realpath) {
    const filePath = localPath(roots.get(item.library_id), item.rel_path);
    const real = onDisk(filePath, realpath);
    const context = item.search_context || {};
    return {
        assetId: item.asset_id,
        name: path.posix.basename(item.rel_path || ''),
        folder: path.posix.dirname(item.rel_path || ''),
        libraryName: item.library_name || '',
        mediaType: item.media_type,
        durationSec: item.duration_sec ?? null,
        snippet: context.snippet || '',
        hitType: context.hit_type || null,
        relPath: item.rel_path,
        localPath: real || filePath,
        onThisMac: Boolean(real),
    };
}

// `media`: 'video', 'image' or 'all'. `inProject`: a Set of the project's
// files by pathKey (paths.js) to leave out, or null to show everything.
async function searchPage({ client, text, media, after = null, inProject = null, realpath = fs.realpathSync.native }) {
    const roots = await client.libraryRoots();
    const items = [];
    let cursor = after;
    let hidden = 0;
    let totalEstimate = null;
    for (let request = 0; request < MAX_REQUESTS; request += 1) {
        const page = await client.search(text, { after: cursor, limit: PAGE_SIZE - items.length, media });
        cursor = page.nextCursor;
        if (totalEstimate === null) totalEstimate = page.totalEstimate;
        for (const item of page.items) {
            const clip = toClip(item, roots, realpath);
            if (inProject && clip.localPath && inProject.has(clip.localPath.normalize('NFC'))) {
                hidden += 1;
            } else {
                items.push(clip);
            }
        }
        if (!inProject || items.length >= PAGE_SIZE || !cursor) break;
    }
    return { items, nextCursor: cursor, totalEstimate, hidden };
}

module.exports = { MAX_REQUESTS, searchPage };
