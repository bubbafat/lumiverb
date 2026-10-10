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
const { alsoAddToProject, newProjectFromResolve } = require('./projects');
const { addAtPlayhead, addToBin, currentProjectName } = require('./resolve');
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

async function search({ text, media, scope, project, after }) {
    const page = await searchPage({ client, text, media, scope, project, after });
    shown = new Map(page.items.map((clip) => [clip.assetId, clip]));
    return page;
}

// The caller's projects, and the open Resolve project's name so the page can
// pick the Lumiverb project of that name (no stored link between them).
async function projects() {
    let resolveProject = null;
    try {
        resolveProject = await currentProjectName(await getResolve());
    } catch {
        // Not knowing it only means nothing is picked.
    }
    return { projects: await client.projects(), resolveProject };
}

function reply(fn) {
    return async (...args) => {
        try {
            return { ok: true, value: await fn(...args) };
        } catch (error) {
            return { ok: false, error: error.message, signedOut: error instanceof SignedOutError };
        }
    };
}

// Every handler answers { ok, value } or { ok: false, error, signedOut }, so
// the page shows the message itself instead of Electron's wrapped one.
function handle(channel, fn) {
    ipcMain.handle(channel, (_event, ...args) => reply(fn)(...args));
}

// The same, for work that reports progress: `fn(progress, ...args)`, where
// progress(p) reaches the page as a 'lumiverb:progress' event.
function handleWithProgress(channel, fn) {
    ipcMain.handle(channel, (event, ...args) => {
        const progress = (p) => event.sender.send('lumiverb:progress', p);
        return reply(fn)(progress, ...args);
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
    handle('lumiverb:projects', projects);
    handleWithProgress('lumiverb:newFromResolve', async (progress) =>
        newProjectFromResolve({ resolve: await getResolve(), client, onProgress: progress }));
    // `projectId`: also add the clip to that Lumiverb project, or null.
    handle('resolve:addToBin', async (assetId, projectId) => {
        const message = await addToBin(await getResolve(), shownClip(assetId).localPath);
        return alsoAddToProject({ client, message, projectId, assetId });
    });
    handle('resolve:addAtPlayhead', async (assetId, projectId) => {
        const clip = shownClip(assetId);
        const message = await addAtPlayhead(await getResolve(), clip.localPath, clip.mediaType);
        return alsoAddToProject({ client, message, projectId, assetId });
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
