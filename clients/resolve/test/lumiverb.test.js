const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const {
    LumiverbClient,
    MAX_IDS,
    SignedOutError,
    createSessionStore,
    localPath,
    normalizeServerUrl,
} = require('../plugin/lumiverb');

function reply(status, body, headers = {}) {
    return {
        ok: status >= 200 && status < 300,
        status,
        headers: { get: (name) => headers[name.toLowerCase()] ?? null },
        json: async () => body,
        arrayBuffer: async () => (Buffer.isBuffer(body) ? body : Buffer.from(JSON.stringify(body))),
    };
}

// A fake fetch: `route(url, init)` answers each call; every call is recorded.
function fakeFetch(route) {
    const calls = [];
    const fetch = async (url, init = {}) => {
        calls.push({ url, init });
        return route(url, init);
    };
    fetch.calls = calls;
    return fetch;
}

function tempStore() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lumiverb-resolve-'));
    const file = path.join(dir, 'session.json');
    return { file, store: createSessionStore(file) };
}

function signedInClient(fetch, token = 'old') {
    const { file, store } = tempStore();
    store.save({ serverUrl: 'http://media', email: 'r@example.com', token });
    return { file, client: new LumiverbClient({ store, fetch }) };
}

const bearer = (init) => (init.headers || {}).Authorization;

test('normalizeServerUrl adds http:// and drops trailing slashes', () => {
    assert.equal(normalizeServerUrl('media'), 'http://media');
    assert.equal(normalizeServerUrl(' http://media:8100/ '), 'http://media:8100');
    assert.equal(normalizeServerUrl('https://lumiverb.example.com'), 'https://lumiverb.example.com');
    assert.throws(() => normalizeServerUrl('  '), /Enter the server address/);
});

test('localPath joins the library root and the clip path', () => {
    assert.equal(localPath('/Volumes/media-01/Media', '2026/Trip/C0001.MP4'), '/Volumes/media-01/Media/2026/Trip/C0001.MP4');
    assert.equal(localPath('/Volumes/media-01/Media', '2026/../../etc/passwd'), null);
    assert.equal(localPath(undefined, 'a.mov'), null);
});

test('signIn keeps the server, email and token, never the password', async () => {
    const fetch = fakeFetch(() => reply(200, { access_token: 'jwt-1', token_type: 'bearer', expires_in: 3600 }));
    const { file, store } = tempStore();
    const client = new LumiverbClient({ store, fetch });

    await client.signIn('media', 'r@example.com', 'hunter2-hunter2');

    assert.equal(fetch.calls[0].url, 'http://media/v1/auth/login');
    assert.deepEqual(JSON.parse(fetch.calls[0].init.body), { email: 'r@example.com', password: 'hunter2-hunter2' });
    assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), { serverUrl: 'http://media', email: 'r@example.com', token: 'jwt-1' });
    assert.ok(!fs.readFileSync(file, 'utf8').includes('hunter2'));
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
    assert.ok(client.signedIn);
});

test("signIn shows the server's error message", async () => {
    const fetch = fakeFetch(() => reply(401, { error: { code: 'invalid_credentials', message: 'Wrong email or password.' } }));
    const client = new LumiverbClient({ store: tempStore().store, fetch });
    await assert.rejects(client.signIn('media', 'r@example.com', 'nope'), /Wrong email or password\./);
    assert.ok(!client.signedIn);
});

test('search asks for 10 clips of a text search, after the given cursor', async () => {
    const fetch = fakeFetch(() => reply(200, { items: [{ asset_id: 'a1' }], next_cursor: 'c2', total_estimate: 31 }));
    const { client } = signedInClient(fetch, 'jwt');

    const page = await client.search('beach sunset', { after: 'c1', media: 'video' });

    const url = new URL(fetch.calls[0].url);
    assert.equal(url.pathname, '/v1/query');
    assert.deepEqual(url.searchParams.getAll('f'), ['query:beach sunset', 'media:video']);
    assert.equal(url.searchParams.get('limit'), '10');
    assert.equal(url.searchParams.get('after'), 'c1');
    assert.equal(bearer(fetch.calls[0].init), 'Bearer jwt');
    assert.deepEqual(page, { items: [{ asset_id: 'a1' }], nextCursor: 'c2', totalEstimate: 31 });
});

test('search filters by type: images, or nothing for all', async () => {
    const fetch = fakeFetch(() => reply(200, { items: [], next_cursor: null }));
    const { client } = signedInClient(fetch, 'jwt');

    await client.search('x', { media: 'image' });
    await client.search('x', { media: 'all' });

    assert.deepEqual(new URL(fetch.calls[0].url).searchParams.getAll('f'), ['query:x', 'media:image']);
    assert.deepEqual(new URL(fetch.calls[1].url).searchParams.getAll('f'), ['query:x']);
    await assert.rejects(client.search('x', { media: 'audio' }), /Unknown type: audio\./);
    assert.equal(fetch.calls.length, 2, 'an unknown type never reaches the server');
});

test('search in a project, or everything but it', async () => {
    const fetch = fakeFetch(() => reply(200, { items: [], next_cursor: null }));
    const { client } = signedInClient(fetch, 'jwt');

    await client.search('x', { scope: 'in', project: 'prj_1' });
    await client.search('x', { scope: 'out', project: 'prj_1' });
    await client.search('x', { scope: 'all', project: 'prj_1' });

    const filters = fetch.calls.map((call) => new URL(call.url).searchParams.getAll('f'));
    assert.deepEqual(filters, [['query:x', 'project:prj_1'], ['query:x', 'not_project:prj_1'], ['query:x']]);
    await assert.rejects(client.search('x', { scope: 'out' }), /Choose a project first\./);
    await assert.rejects(client.search('x', { scope: 'near', project: 'prj_1' }), /Unknown scope: near\./);
    assert.equal(fetch.calls.length, 3, 'neither reaches the server');
});

test('projects lists yours and shared ones', async () => {
    const fetch = fakeFetch(() => reply(200, { items: [
        { project_id: 'prj_1', name: 'NCL Jewel Walkthrough', ownership: 'own', asset_count: 340, updated_at: '2026-10-10T20:00:00Z' },
        { project_id: 'prj_2', name: 'Shared', ownership: 'shared', asset_count: 2, updated_at: '2026-10-01T00:00:00Z' },
    ] }));
    const { client } = signedInClient(fetch, 'jwt');

    assert.deepEqual(await client.projects(), [
        { projectId: 'prj_1', name: 'NCL Jewel Walkthrough', own: true, assetCount: 340, updatedAt: '2026-10-10T20:00:00Z' },
        { projectId: 'prj_2', name: 'Shared', own: false, assetCount: 2, updatedAt: '2026-10-01T00:00:00Z' },
    ]);
    assert.equal(fetch.calls[0].url, 'http://media/v1/projects');
});

test('createProject sends the clips in order, past the server limit in a second request', async () => {
    const ids = Array.from({ length: MAX_IDS + 1 }, (_, i) => `a${i}`);
    const fetch = fakeFetch((url, init) => (url.endsWith('/v1/projects')
        ? reply(201, { project_id: 'prj_new', name: 'Trip' })
        : reply(200, { added: JSON.parse(init.body).asset_ids.length })));
    const { client } = signedInClient(fetch, 'jwt');

    assert.deepEqual(await client.createProject('Trip', ids), { projectId: 'prj_new', name: 'Trip', own: true });

    const [create, add] = fetch.calls;
    assert.equal(create.init.method, 'POST');
    assert.equal(create.init.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(create.init.body), { name: 'Trip', asset_ids: ids.slice(0, MAX_IDS) });
    assert.equal(add.url, 'http://media/v1/projects/prj_new/assets');
    assert.deepEqual(JSON.parse(add.init.body), { asset_ids: [ids[MAX_IDS]] });
});

test('addToProject says how many were new to the project', async () => {
    const fetch = fakeFetch(() => reply(200, { added: 0 }));
    const { client } = signedInClient(fetch, 'jwt');
    assert.equal(await client.addToProject('prj_1', ['a1']), 0);
    assert.deepEqual(JSON.parse(fetch.calls[0].init.body), { asset_ids: ['a1'] });
});

test('assetIdAt finds a clip by its library and path, or null', async () => {
    const fetch = fakeFetch((url) => (url.includes('rel_path=2026%2Fa.mov')
        ? reply(200, { asset_id: 'a1' })
        : reply(404, { error: { code: 'not_found', message: 'Asset not found' } })));
    const { client } = signedInClient(fetch, 'jwt');

    assert.equal(await client.assetIdAt('lib1', '2026/a.mov'), 'a1');
    assert.equal(await client.assetIdAt('lib1', '2026/gone.mov'), null);
    const url = new URL(fetch.calls[0].url);
    assert.equal(url.pathname, '/v1/assets/by-path');
    assert.equal(url.searchParams.get('library_id'), 'lib1');
});

test('a 401 refreshes the token once and retries with the new one', async () => {
    const fetch = fakeFetch((url, init) => {
        if (url.endsWith('/v1/auth/refresh')) return reply(200, { access_token: 'new' });
        return bearer(init) === 'Bearer new' ? reply(200, []) : reply(401, { error: { code: 'token_expired', message: 'Expired.' } });
    });
    const { file, client } = signedInClient(fetch);

    await client.libraryRoots();

    assert.deepEqual(fetch.calls.map((call) => call.url.replace('http://media', '')), ['/v1/libraries', '/v1/auth/refresh', '/v1/libraries']);
    assert.equal(bearer(fetch.calls[1].init), 'Bearer old');
    assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).token, 'new');
});

test('requests that 401 together share one refresh', async () => {
    const fetch = fakeFetch((url, init) => {
        if (url.endsWith('/v1/auth/refresh')) return reply(200, { access_token: 'new' });
        if (bearer(init) !== 'Bearer new') return reply(401, {});
        return reply(200, Buffer.from('img'), { 'content-type': 'image/webp' });
    });
    const { client } = signedInClient(fetch);

    const thumbs = await Promise.all(['a1', 'a2', 'a3'].map((id) => client.thumbnail(id)));

    assert.equal(fetch.calls.filter((call) => call.url.endsWith('/v1/auth/refresh')).length, 1);
    assert.deepEqual(thumbs, Array(3).fill(`data:image/webp;base64,${Buffer.from('img').toString('base64')}`));
});

test('a refused refresh signs out but keeps the server and email', async () => {
    const fetch = fakeFetch(() => reply(401, {}));
    const { file, client } = signedInClient(fetch);

    await assert.rejects(client.search('x'), SignedOutError);

    assert.ok(!client.signedIn);
    assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), { serverUrl: 'http://media', email: 'r@example.com' });
});

test('a thumbnail that is not made yet is null', async () => {
    const fetch = fakeFetch(() => reply(404, { error: { code: 'artifact_not_ready', message: 'Not ready.' } }));
    const { client } = signedInClient(fetch, 'jwt');
    assert.equal(await client.thumbnail('a1'), null);
    // By its name (ADR-024 phase 1 part 2).
    assert.equal(fetch.calls[0].url, 'http://media/v1/assets/a1/artifacts/lumiverb.preview.thumbnail');
});

test('libraryRoots maps each library to its root, read once', async () => {
    const fetch = fakeFetch(() => reply(200, [{ library_id: 'lib1', root_path: '/Volumes/media-01/Media' }]));
    const { client } = signedInClient(fetch, 'jwt');
    await client.libraryRoots();
    const roots = await client.libraryRoots();
    assert.equal(roots.get('lib1'), '/Volumes/media-01/Media');
    assert.equal(fetch.calls.length, 1);
});
