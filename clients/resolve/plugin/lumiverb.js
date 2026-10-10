// Lumiverb API client for the Resolve plugin's main process.
//
// All HTTP happens here, in the main process: the page never sees the token,
// and requests to the server aren't subject to the page's CORS. Kept free of
// Electron imports so tests drive it with a fake fetch and a temp folder.

const fs = require('fs');
const path = require('path');

const PAGE_SIZE = 10;

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

    async request(apiPath) {
        if (!this.signedIn) throw new SignedOutError('Sign in first.');
        for (let attempt = 0; attempt < 2; attempt += 1) {
            const token = this.session.token;
            let response;
            try {
                response = await this.fetch(`${this.session.serverUrl}${apiPath}`, {
                    headers: { Authorization: `Bearer ${token}` },
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
    // previous page's next_cursor.
    async search(text, after = null) {
        const params = new URLSearchParams();
        params.append('f', `query:${text}`);
        params.append('limit', String(PAGE_SIZE));
        if (after) params.append('after', after);
        const response = await this.request(`/v1/query?${params}`);
        const body = await response.json();
        return {
            items: body.items || [],
            nextCursor: body.next_cursor || null,
            totalEstimate: body.total_estimate ?? null,
        };
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

    // The clip's thumbnail as a data URL, or null when it isn't made yet.
    async thumbnail(assetId) {
        try {
            const response = await this.request(`/v1/assets/${encodeURIComponent(assetId)}/artifacts/thumbnail`);
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
    PAGE_SIZE,
    LumiverbClient,
    LumiverbError,
    SignedOutError,
    createSessionStore,
    localPath,
    normalizeServerUrl,
};
