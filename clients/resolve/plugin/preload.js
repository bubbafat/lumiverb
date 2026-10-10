// The page's only way to the main process. Each call answers
// { ok, value } or { ok: false, error, signedOut }.

const { contextBridge, ipcRenderer } = require('electron/renderer');

contextBridge.exposeInMainWorld('lumiverb', {
    session: () => ipcRenderer.invoke('session:get'),
    signIn: (serverUrl, email, password) => ipcRenderer.invoke('session:signIn', serverUrl, email, password),
    signOut: () => ipcRenderer.invoke('session:signOut'),
    // { text, media: 'video' | 'image' | 'all', scope: 'all' | 'in' | 'out', project, after }
    search: (options) => ipcRenderer.invoke('lumiverb:search', options),
    thumbnail: (assetId) => ipcRenderer.invoke('lumiverb:thumbnail', assetId),
    projects: () => ipcRenderer.invoke('lumiverb:projects'),
    newFromResolve: () => ipcRenderer.invoke('lumiverb:newFromResolve'),
    // callback({ step, done, total }) while newFromResolve runs. Returns a
    // function that stops listening.
    onProgress: (callback) => {
        const listener = (_event, progress) => callback(progress);
        ipcRenderer.on('lumiverb:progress', listener);
        return () => ipcRenderer.removeListener('lumiverb:progress', listener);
    },
    // projectId: also add the clip to that Lumiverb project, or null.
    addToBin: (assetId, projectId) => ipcRenderer.invoke('resolve:addToBin', assetId, projectId || null),
    addAtPlayhead: (assetId, projectId) => ipcRenderer.invoke('resolve:addAtPlayhead', assetId, projectId || null),
});
