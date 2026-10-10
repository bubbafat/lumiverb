const assert = require('node:assert/strict');
const { test } = require('node:test');

const { searchPage } = require('../plugin/search');

const ROOT = '/Volumes/media-01/Media';

// A fake Lumiverb with `count` results; its cursor is the index of the next
// result. Records each request's options.
function fakeClient(count) {
    const results = Array.from({ length: count }, (_, i) => ({
        asset_id: `a${i}`,
        library_id: 'lib',
        library_name: 'Media',
        rel_path: `2026/clip${i}.mov`,
        media_type: 'video',
        duration_sec: 10,
        search_context: { hit_type: 'transcript', snippet: `said ${i}` },
    }));
    const requests = [];
    return {
        requests,
        libraryRoots: async () => new Map([['lib', ROOT]]),
        search: async (text, options) => {
            requests.push({ text, ...options });
            const start = options.after ? Number(options.after) : 0;
            const end = Math.min(start + 10, count);
            return { items: results.slice(start, end), nextCursor: end < count ? String(end) : null, totalEstimate: count };
        },
    };
}

// A fake realpath: every file is there, spelled as given.
const everywhere = (p) => p;

test('a page is the clips the server answers, with their paths on this Mac', async () => {
    const client = fakeClient(25);
    const missing = `${ROOT}/2026/clip1.mov`;
    const realpath = (p) => {
        if (p === missing) throw new Error('ENOENT');
        return p;
    };

    const page = await searchPage({ client, text: 'harbor', media: 'video', realpath });

    assert.equal(page.items.length, 10);
    assert.equal(page.nextCursor, '10');
    assert.equal(page.totalEstimate, 25);
    assert.deepEqual(page.items[0], {
        assetId: 'a0', name: 'clip0.mov', folder: '2026', libraryName: 'Media', mediaType: 'video',
        durationSec: 10, snippet: 'said 0', hitType: 'transcript', relPath: '2026/clip0.mov',
        localPath: `${ROOT}/2026/clip0.mov`, onThisMac: true,
    });
    assert.equal(page.items[1].onThisMac, false);
    assert.equal(page.items[1].localPath, missing);
});

test('the type, scope, project and cursor go to the server as they are', async () => {
    const client = fakeClient(25);
    await searchPage({ client, text: 'harbor', media: 'image', scope: 'out', project: 'prj_1', after: '10', realpath: everywhere });
    assert.deepEqual(client.requests, [{ text: 'harbor', after: '10', media: 'image', scope: 'out', project: 'prj_1' }]);
});

test('a clip on this Mac is imported by the path the disk spells, not the library root', async () => {
    // The library root says /Volumes/media-01; the disk is mounted as
    // /Volumes/MEDIA-01, which is what Resolve stores.
    const client = fakeClient(1);
    const disk = (p) => p.replace('/Volumes/media-01/', '/Volumes/MEDIA-01/');
    const page = await searchPage({ client, text: 'x', realpath: disk });
    assert.equal(page.items[0].localPath, '/Volumes/MEDIA-01/Media/2026/clip0.mov');
});
