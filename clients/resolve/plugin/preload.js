// The page's only way to the main process. Each call answers
// { ok, value } or { ok: false, error, signedOut }.

const { contextBridge, ipcRenderer } = require('electron/renderer');

contextBridge.exposeInMainWorld('lumiverb', {
    session: () => ipcRenderer.invoke('session:get'),
    signIn: (serverUrl, email, password) => ipcRenderer.invoke('session:signIn', serverUrl, email, password),
    signOut: () => ipcRenderer.invoke('session:signOut'),
    search: (text, after, hideInProject) => ipcRenderer.invoke('lumiverb:search', text, after, hideInProject),
    thumbnail: (assetId) => ipcRenderer.invoke('lumiverb:thumbnail', assetId),
    addToBin: (assetId) => ipcRenderer.invoke('resolve:addToBin', assetId),
    addAtPlayhead: (assetId) => ipcRenderer.invoke('resolve:addAtPlayhead', assetId),
});
