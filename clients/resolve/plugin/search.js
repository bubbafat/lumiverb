// One page of results for the plugin, each clip with its path on this Mac.
// In or out of a project is the server's filter (f=project: /
// f=not_project:), so a page is exactly what the server answers.

const fs = require('fs');
const path = require('path');
const { localPath } = require('./lumiverb');
const { onDisk } = require('./paths');

// `localPath` is the file's real path when it's on this Mac: what's imported.
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

// `media`: 'video', 'image' or 'all'; `scope`: 'all', 'in' or 'out' of
// `project`. `after` is a place in the search's order, so a page asked for
// with other filters picks up where the last one was.
async function searchPage({ client, text, media, scope = 'all', project = null, after = null, realpath = fs.realpathSync.native }) {
    const roots = await client.libraryRoots();
    const page = await client.search(text, { after, media, scope, project });
    return {
        items: page.items.map((item) => toClip(item, roots, realpath)),
        nextCursor: page.nextCursor,
        totalEstimate: page.totalEstimate,
    };
}

module.exports = { searchPage };
