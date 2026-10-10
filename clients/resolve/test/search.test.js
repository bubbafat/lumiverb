const assert = require('node:assert/strict');
const { test } = require('node:test');

const { MAX_REQUESTS, searchPage } = require('../plugin/search');

const ROOT = '/Volumes/media-01/Media';

// A fake Lumiverb with `count` results; its cursor is the index of the next
// result. Records each request's cursor, limit and type.
function fakeClient(count, { relPath = (i) => `2026/clip${i}.mov` } = {}) {
    const results = Array.from({ length: count }, (_, i) => ({
        asset_id: `a${i}`,
        library_id: 'lib',
        library_name: 'Media',
        rel_path: relPath(i),
        media_type: 'video',
        duration_sec: 10,
        search_context: { hit_type: 'transcript', snippet: `said ${i}` },
    }));
    const requests = [];
    return {
        requests,
        libraryRoots: async () => new Map([['lib', ROOT]]),
        search: async (text, { after, limit, media }) => {
            requests.push({ text, after, limit, media });
            const start = after ? Number(after) : 0;
            const end = Math.min(start + limit, count);
            return { items: results.slice(start, end), nextCursor: end < count ? String(end) : null, totalEstimate: count };
        },
    };
}

// A fake realpath: every file is there, spelled as given.
const everywhere = (p) => p;
const ids = (page) => page.items.map((clip) => clip.assetId);

test('a page is 10 clips with their paths on this Mac', async () => {
    const client = fakeClient(25);
    const page = await searchPage({ client, text: 'harbor', media: 'video', realpath: (p) => { if (p === `${ROOT}/2026/clip1.mov`) throw new Error('ENOENT'); return p; } });

    assert.deepEqual(client.requests, [{ text: 'harbor', after: null, limit: 10, media: 'video' }]);
    assert.equal(page.items.length, 10);
    assert.equal(page.nextCursor, '10');
    assert.equal(page.totalEstimate, 25);
    assert.equal(page.hidden, 0);
    assert.deepEqual(page.items[0], {
        assetId: 'a0', name: 'clip0.mov', folder: '2026', libraryName: 'Media', mediaType: 'video',
        durationSec: 10, snippet: 'said 0', hitType: 'transcript', relPath: '2026/clip0.mov',
        localPath: `${ROOT}/2026/clip0.mov`, onThisMac: true,
    });
    assert.equal(page.items[1].onThisMac, false);
});

test('clips already in the project are hidden and the page is filled up to 10', async () => {
    const client = fakeClient(25);
    const inProject = new Set([2, 5, 7, 11].map((i) => `${ROOT}/2026/clip${i}.mov`));

    const page = await searchPage({ client, text: 'harbor', media: 'image', inProject, realpath: everywhere });

    assert.deepEqual(client.requests.map((r) => [r.after, r.limit, r.media]), [[null, 10, 'image'], ['10', 3, 'image'], ['13', 1, 'image']]);
    assert.deepEqual(ids(page), ['a0', 'a1', 'a3', 'a4', 'a6', 'a8', 'a9', 'a10', 'a12', 'a13']);
    assert.equal(page.hidden, 4);
    assert.equal(page.nextCursor, '14', 'the next page starts right after the last clip looked at');
});

test('the next page carries on from the cursor without skipping or repeating', async () => {
    const client = fakeClient(25);
    const inProject = new Set([3].map((i) => `${ROOT}/2026/clip${i}.mov`));
    const first = await searchPage({ client, text: 'x', inProject, realpath: everywhere });
    const second = await searchPage({ client, text: 'x', after: first.nextCursor, inProject, realpath: everywhere });
    const third = await searchPage({ client, text: 'x', after: second.nextCursor, inProject, realpath: everywhere });

    const all = [...ids(first), ...ids(second), ...ids(third)];
    assert.deepEqual(all, Array.from({ length: 25 }, (_, i) => `a${i}`).filter((id) => id !== 'a3'));
    assert.equal(third.nextCursor, null);
});

test('a path in the project matches whichever Unicode form either side uses', async () => {
    const nfd = 'Lisboa/Café.mov';
    const client = fakeClient(1, { relPath: () => nfd });
    const inProject = new Set([`${ROOT}/Lisboa/Café.mov`]);
    const page = await searchPage({ client, text: 'x', inProject, realpath: everywhere });
    assert.equal(page.hidden, 1);
    assert.equal(page.items.length, 0);
});

test('a clip is matched by the path the disk spells, not the library root', async () => {
    // The library root says /Volumes/media-01; the disk is mounted as
    // /Volumes/MEDIA-01, which is what Resolve stores.
    const client = fakeClient(2);
    const disk = (p) => p.replace('/Volumes/media-01/', '/Volumes/MEDIA-01/');
    const inProject = new Set(['/Volumes/MEDIA-01/Media/2026/clip0.mov']);

    const page = await searchPage({ client, text: 'x', inProject, realpath: disk });

    assert.deepEqual(ids(page), ['a1']);
    assert.equal(page.hidden, 1);
    assert.equal(page.items[0].localPath, '/Volumes/MEDIA-01/Media/2026/clip1.mov', 'imported by the path the disk spells');
});

test(`a search that is nearly all in the project stops after ${MAX_REQUESTS} requests`, async () => {
    const client = fakeClient(500);
    const inProject = new Set(Array.from({ length: 500 }, (_, i) => `${ROOT}/2026/clip${i}.mov`));

    const page = await searchPage({ client, text: 'x', inProject, realpath: everywhere });

    assert.equal(client.requests.length, MAX_REQUESTS);
    assert.equal(page.items.length, 0);
    assert.equal(page.hidden, 100);
    assert.equal(page.nextCursor, '100', 'Next still goes on from where it stopped');
});

test('without hiding, a short last page is just shorter', async () => {
    const client = fakeClient(4);
    const page = await searchPage({ client, text: 'x', realpath: everywhere });
    assert.equal(client.requests.length, 1);
    assert.equal(page.items.length, 4);
    assert.equal(page.nextCursor, null);
});
