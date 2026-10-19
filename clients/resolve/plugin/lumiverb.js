// Lumiverb API client for the Resolve plugin's main process.
//
// All HTTP happens here, in the main process: the page never sees the token,
// and requests to the server aren't subject to the page's CORS. Kept free of
// Electron imports so tests drive it with a fake fetch and a temp folder.

const fs = require('fs');
const path = require('path');

const PAGE_SIZE = 10;

// The type choices and the `media:` filter each adds to a search.
const MEDIA_FILTERS = { video: 'video', image: 'image', all: null };

// The scope choices and the project filter each adds (with the project's id).
const SCOPE_FILTERS = { all: null, in: 'project', out: 'not_project' };

// The most asset ids the server takes in one request.
const MAX_IDS = 10000;

class LumiverbError extends Error {
    constructor(message, { status = null, code = null } = {}) {
        super(message);
        this.status = status;
        this.code = code;
    }
}

// The session ended (sign-in expired or revoked): the page goes back to Sign in.
class SignedOutError extends LumiverbError {}

// "media", "media:8100/" -> "http://media", "http://media:8100"
function normalizeServerUrl(input) {
    let url = String(input || '').trim();
    if (!url) throw new LumiverbError('Enter the server address.');
    if (!/^https?:\/\//i.test(url)) url = `http://${url}`;
    return url.replace(/\/+$/, '');
}

// The original's path on this Mac: the library root (stored as the Mac sees
// it) plus the clip's path in the library. Null for a path that climbs out.
function localPath(rootPath, relPath) {
    if (!rootPath || !relPath) return null;
    if (relPath.split('/').some((part) => part === '..')) return null;
    return `${rootPath.replace(/\/+$/, '')}/${relPath.replace(/^\/+/, '')}`;
}

// The session file: server, email and the current token. Never the password.
// Mode 0600, written whole (temp file + rename), like the macOS app's
// FileTokenStore.
function createSessionStore(filePath) {
    return {
        load() {
            try {
                return JSON.parse(fs.readFileSync(filePath, 'utf8'));
            } catch {
                return null;
            }
        },
        save(session) {
            fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
            const tmp = `${filePath}.${process.pid}.tmp`;
            fs.writeFileSync(tmp, JSON.stringify(session), { mode: 0o600 });
            fs.renameSync(tmp, filePath);
        },
    };
}

async function errorFrom(response) {
    let message = `The server answered ${response.status}.`;
    let code = null;
    try {
        const body = await response.json();
        if (body && body.error) {
            message = body.error.message || message;
            code = body.error.code || null;
        }
    } catch {
        // Not JSON: keep the status line.
    }
    return new LumiverbError(message, { status: response.status, code });
}

class LumiverbClient {
    constructor({ store, fetch = globalThis.fetch }) {
        this.store = store;
        this.fetch = fetch;
        this.session = store.load() || {};
        this.refreshing = null;
        this.roots = null;
    }

    get signedIn() {
        return Boolean(this.session.token);
    }

    async signIn(serverUrl, email, password) {
        const server = normalizeServerUrl(serverUrl);
        let response;
        try {
            response = await this.fetch(`${server}/v1/auth/login`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password }),
            });
        } catch {
            throw new LumiverbError(`Couldn't reach ${server}.`);
        }
        if (!response.ok) throw await errorFrom(response);
        const { access_token: token } = await response.json();
        this.session = { serverUrl: server, email, token };
        this.store.save(this.session);
        this.roots = null;
    }

    async signOut() {
        if (this.session.token) {
            try {
                await this.fetch(`${this.session.serverUrl}/v1/auth/logout`, {
                    method: 'POST',
                    headers: { Authorization: `Bearer ${this.session.token}` },
                });
            } catch {
                // Signing out here still works when the server can't be reached.
            }
        }
        this.forgetToken();
    }

    forgetToken() {
        this.session = { serverUrl: this.session.serverUrl, email: this.session.email };
        this.store.save(this.session);
        this.roots = null;
    }

    // A token refreshes once, so concurrent 401s share one refresh: a request
    // whose token was already replaced just retries with the new one.
    async refresh(staleToken) {
        if (this.session.token !== staleToken) return;
        if (!this.refreshing) {
            this.refreshing = (async () => {
                let response;
                try {
                    response = await this.fetch(`${this.session.serverUrl}/v1/auth/refresh`, {
                        method: 'POST',
                        headers: { Authorization: `Bearer ${staleToken}` },
                    });
                } catch {
                    throw new LumiverbError(`Couldn't reach ${this.session.serverUrl}.`);
                }
                if (!response.ok) {
                    this.forgetToken();
                    throw new SignedOutError('Signed out. Sign in again.', { status: response.status });
                }
                const { access_token: token } = await response.json();
                this.session = { ...this.session, token };
                this.store.save(this.session);
            })().finally(() => {
                this.refreshing = null;
            });
        }
        await this.refreshing;
    }

    // `body` is sent as JSON.
    async request(apiPath, { method = 'GET', body } = {}) {
        if (!this.signedIn) throw new SignedOutError('Sign in first.');
        for (let attempt = 0; attempt < 2; attempt += 1) {
            const token = this.session.token;
            const headers = { Authorization: `Bearer ${token}` };
            if (body !== undefined) headers['Content-Type'] = 'application/json';
            let response;
            try {
                response = await this.fetch(`${this.session.serverUrl}${apiPath}`, {
                    method,
                    headers,
                    body: body === undefined ? undefined : JSON.stringify(body),
                });
            } catch {
                throw new LumiverbError(`Couldn't reach ${this.session.serverUrl}.`);
            }
            if (response.status === 401 && attempt === 0) {
                await this.refresh(token);
                continue;
            }
            if (response.status === 401) {
                this.forgetToken();
                throw new SignedOutError('Signed out. Sign in again.', { status: 401 });
            }
            if (!response.ok) throw await errorFrom(response);
            return response;
        }
        throw new LumiverbError('Unreachable');
    }

    // One page of a text search, most relevant first. `after` is the
    // previous page's next_cursor; `media` one of MEDIA_FILTERS' keys;
    // `scope` one of SCOPE_FILTERS' keys, for `project` (its id).
    async search(text, { after = null, limit = PAGE_SIZE, media = 'all', scope = 'all', project = null } = {}) {
        if (!Object.hasOwn(MEDIA_FILTERS, media)) throw new LumiverbError(`Unknown type: ${media}.`);
        if (!Object.hasOwn(SCOPE_FILTERS, scope)) throw new LumiverbError(`Unknown scope: ${scope}.`);
        if (SCOPE_FILTERS[scope] && !project) throw new LumiverbError('Choose a project first.');
        const params = new URLSearchParams();
        params.append('f', `query:${text}`);
        if (MEDIA_FILTERS[media]) params.append('f', `media:${MEDIA_FILTERS[media]}`);
        if (SCOPE_FILTERS[scope]) params.append('f', `${SCOPE_FILTERS[scope]}:${project}`);
        params.append('limit', String(limit));
        if (after) params.append('after', after);
        const response = await this.request(`/v1/query?${params}`);
        const body = await response.json();
        return {
            items: body.items || [],
            nextCursor: body.next_cursor || null,
            totalEstimate: body.total_estimate ?? null,
        };
    }

    // The caller's projects: their own and ones shared with them. Only an
    // owner can add clips (`own`).
    async projects() {
        const response = await this.request('/v1/projects');
        const body = await response.json();
        return (body.items || []).map((project) => ({
            projectId: project.project_id,
            name: project.name,
            own: project.ownership === 'own',
            assetCount: project.asset_count,
            updatedAt: project.updated_at,
        }));
    }

    // A new project holding these clips, in this order. The server takes
    // MAX_IDS at a time, so the rest are added after.
    async createProject(name, assetIds) {
        const response = await this.request('/v1/projects', {
            method: 'POST',
            body: { name, asset_ids: assetIds.slice(0, MAX_IDS) },
        });
        const project = await response.json();
        await this.addToProject(project.project_id, assetIds.slice(MAX_IDS));
        return { projectId: project.project_id, name: project.name, own: true };
    }

    // How many were added: clips already in the project aren't counted.
    async addToProject(projectId, assetIds) {
        let added = 0;
        for (let start = 0; start < assetIds.length; start += MAX_IDS) {
            const response = await this.request(`/v1/projects/${encodeURIComponent(projectId)}/assets`, {
                method: 'POST',
                body: { asset_ids: assetIds.slice(start, start + MAX_IDS) },
            });
            added += (await response.json()).added || 0;
        }
        return added;
    }

    // The asset at a path in a library, or null when Lumiverb has none there.
    async assetIdAt(libraryId, relPath) {
        const params = new URLSearchParams({ library_id: libraryId, rel_path: relPath });
        try {
            const response = await this.request(`/v1/assets/by-path?${params}`);
            return (await response.json()).asset_id;
        } catch (error) {
            if (error.status === 404) return null;
            throw error;
        }
    }

    // library_id -> root_path, read once per session.
    async libraryRoots() {
        if (!this.roots) {
            const response = await this.request('/v1/libraries');
            const libraries = await response.json();
            this.roots = new Map(libraries.map((lib) => [lib.library_id, lib.root_path]));
        }
        return this.roots;
    }

    // The clip's thumbnail as a data URL, or null when it isn't made yet. Asked for by its name (ADR-024:
    // every server from v0.1.105 serves it so).
    async thumbnail(assetId) {
        try {
            const response = await this.request(
                `/v1/assets/${encodeURIComponent(assetId)}/artifacts/lumiverb.preview.thumbnail`);
            const type = response.headers.get('content-type') || 'image/webp';
            const bytes = Buffer.from(await response.arrayBuffer());
            return `data:${type};base64,${bytes.toString('base64')}`;
        } catch (error) {
            if (error.status === 404) return null;
            throw error;
        }
    }
}

module.exports = {
    MAX_IDS,
    MEDIA_FILTERS,
    PAGE_SIZE,
    SCOPE_FILTERS,
    LumiverbClient,
    LumiverbError,
    SignedOutError,
    createSessionStore,
    localPath,
    normalizeServerUrl,
};
