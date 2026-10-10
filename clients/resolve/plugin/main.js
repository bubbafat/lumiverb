// Lumiverb Workflow Integration for DaVinci Resolve Studio: the main process.
//
// Resolve runs this as an Electron app (Workspace > Workflow Integrations >
// Lumiverb). The page is sandboxed; it reaches this process only through the
// handlers below (see preload.js). Lumiverb calls and Resolve calls both
// happen here.

const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const WorkflowIntegration = require('./WorkflowIntegration.node');
const { LumiverbClient, SignedOutError, createSessionStore } = require('./lumiverb');
const { addAtPlayhead, addToBin, projectFilePaths } = require('./resolve');
const { searchPage } = require('./search');

const PLUGIN_ID = 'io.lumiverb.resolve';

const client = new LumiverbClient({
    store: createSessionStore(path.join(app.getPath('appData'), PLUGIN_ID, 'session.json')),
});

// The clips on the page, by asset id. The page names a clip by its id; the
// path imported is always the one worked out here.
let shown = new Map();

let resolveObject = null;
async function getResolve() {
    if (!resolveObject) {
        if (!(await WorkflowIntegration.Initialize(PLUGIN_ID))) throw new Error("Couldn't connect to Resolve.");
        resolveObject = await WorkflowIntegration.GetResolve();
        if (!resolveObject) throw new Error("Couldn't connect to Resolve.");
    }
    return resolveObject;
}

function shownClip(assetId) {
    const clip = shown.get(assetId);
    if (!clip) throw new Error('Search again: that result is gone.');
    if (!clip.onThisMac) throw new Error(`Not on this Mac: ${clip.localPath || clip.relPath}`);
    return clip;
}

// The project's files are read again for every page, so clips added in
// Resolve meanwhile are hidden too.
async function search({ text, media, after, hideInProject }) {
    const inProject = hideInProject ? await projectFilePaths(await getResolve()) : null;
    const page = await searchPage({ client, text, media, after, inProject });
    shown = new Map(page.items.map((clip) => [clip.assetId, clip]));
    return page;
}

// Every handler answers { ok, value } or { ok: false, error, signedOut }, so
// the page shows the message itself instead of Electron's wrapped one.
function handle(channel, fn) {
    ipcMain.handle(channel, async (_event, ...args) => {
        try {
            return { ok: true, value: await fn(...args) };
        } catch (error) {
            return { ok: false, error: error.message, signedOut: error instanceof SignedOutError };
        }
    });
}

function registerHandlers() {
    handle('session:get', () => ({
        serverUrl: client.session.serverUrl || '',
        email: client.session.email || '',
        signedIn: client.signedIn,
    }));
    handle('session:signIn', (serverUrl, email, password) => client.signIn(serverUrl, email, password));
    handle('session:signOut', () => client.signOut());
    handle('lumiverb:search', search);
    handle('lumiverb:thumbnail', (assetId) => client.thumbnail(assetId));
    handle('resolve:addToBin', async (assetId) => addToBin(await getResolve(), shownClip(assetId).localPath));
    handle('resolve:addAtPlayhead', async (assetId) => {
        const clip = shownClip(assetId);
        return addAtPlayhead(await getResolve(), clip.localPath, clip.mediaType);
    });
}

function createWindow() {
    const window = new BrowserWindow({
        width: 560,
        height: 760,
        title: 'Lumiverb',
        backgroundColor: '#1c1c1e',
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            sandbox: true,
            nodeIntegration: false,
        },
    });
    window.webContents.on('will-navigate', (event) => event.preventDefault());
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    window.on('close', () => app.quit());
    window.loadFile(path.join(__dirname, 'index.html'));
}

app.whenReady().then(() => {
    registerHandlers();
    createWindow();
});

app.on('will-quit', () => {
    if (resolveObject) WorkflowIntegration.CleanUp();
});
