// What the plugin does in Resolve: add a file to the current bin, put it on
// the current timeline at the playhead, or list the files the project has.
//
// Every function takes the `resolve` object (WorkflowIntegration.GetResolve())
// so tests drive them with a fake. Targets the Resolve 21.1 scripting API
// (GetSettings / GetClipProperty() dicts, clipInfo lists).

const fs = require('fs');
const { pathKey } = require('./paths');

// The frame number of a timecode on a timeline at `fps`, counted the way
// Resolve numbers timeline frames (01:00:00:00 at 24 fps is 86400). Drop-frame
// (29.97, 59.94) skips the first 2 or 4 frame numbers of each minute but
// every tenth.
function timecodeToFrame(timecode, fps, dropFrame = false) {
    const match = /^(\d{1,2})[:;](\d{2})[:;](\d{2})[:;.](\d{2,3})$/.exec(String(timecode).trim());
    if (!match || !(fps > 0)) throw new Error(`Can't read the playhead timecode ${timecode}.`);
    const [hours, minutes, seconds, frames] = match.slice(1).map(Number);
    const nominal = Math.round(fps);
    let frame = (hours * 3600 + minutes * 60 + seconds) * nominal + frames;
    if (dropFrame && (nominal === 30 || nominal === 60)) {
        const dropped = nominal / 15;
        const totalMinutes = hours * 60 + minutes;
        frame -= dropped * (totalMinutes - Math.floor(totalMinutes / 10));
    }
    return frame;
}

// A clip's length in timeline frames. Resolve converts source frames to
// timeline frames by flooring. Infinity when the clip doesn't say, so the
// track chosen is clear from the playhead on.
function timelineFrames(clipFrames, clipFps, timelineFps) {
    if (!(clipFrames > 0 && clipFps > 0 && timelineFps > 0)) return Infinity;
    return Math.floor((clipFrames * timelineFps) / clipFps);
}

function overlaps(spans, start, end) {
    return spans.some((span) => span.start < end && start < span.end);
}

// The first track index free from `start` for `duration` frames: its video
// track, and its audio track too when the clip has sound. Tracks are lists of
// { start, end } spans (end exclusive). An index past the last track means
// "add a track". AppendToTimeline never overwrites, it drops a clip that
// lands on another, so the free track is found first.
function pickTrack({ video, audio = [], start, duration, needsAudio = false }) {
    const end = start + duration;
    const last = Math.max(video.length, needsAudio ? audio.length : 0) + 1;
    for (let index = 1; index < last; index += 1) {
        const videoFree = index > video.length || !overlaps(video[index - 1], start, end);
        const audioFree = !needsAudio || index > audio.length || !overlaps(audio[index - 1], start, end);
        if (videoFree && audioFree) return index;
    }
    return last;
}

async function currentProject(resolve) {
    const manager = await resolve.GetProjectManager();
    const project = manager && (await manager.GetCurrentProject());
    if (!project) throw new Error('Open a project in Resolve first.');
    return project;
}

// A clip's original by pathKey (paths.js), so it matches Lumiverb's path for
// the same file however each spells it. Null for a clip with no file (a
// timeline).
async function clipFileKey(clip, realpath) {
    const properties = await clip.GetClipProperty();
    const filePath = properties && properties['File Path'];
    return filePath ? pathKey(filePath, realpath) : null;
}

// The originals of every clip in the current project's Media Pool, every
// bin (bin by bin, as listed), by pathKey. `onProgress(read)` after each clip.
async function projectFilePaths(resolve, realpath = fs.realpathSync.native, onProgress = () => {}) {
    const project = await currentProject(resolve);
    const mediaPool = await project.GetMediaPool();
    const paths = new Set();
    const folders = [await mediaPool.GetRootFolder()];
    let read = 0;
    while (folders.length > 0) {
        const folder = folders.shift();
        for (const clip of (await folder.GetClipList()) || []) {
            const key = await clipFileKey(clip, realpath);
            if (key) paths.add(key);
            read += 1;
            onProgress(read);
        }
        folders.push(...((await folder.GetSubFolderList()) || []));
    }
    return paths;
}

// The open Resolve project's name, or null when none is open.
async function currentProjectName(resolve) {
    const manager = await resolve.GetProjectManager();
    const project = manager && (await manager.GetCurrentProject());
    return project ? project.GetName() : null;
}

// The file's clip in the current bin: the one already there, or imported now.
async function clipInCurrentBin(mediaPool, filePath, realpath) {
    const bin = await mediaPool.GetCurrentFolder();
    if (!bin) throw new Error('Select a bin in the Media Pool first.');
    const wanted = pathKey(filePath, realpath);
    for (const clip of (await bin.GetClipList()) || []) {
        if ((await clipFileKey(clip, realpath)) === wanted) return { clip, bin, added: false };
    }
    // Plain paths. The 21.1 docs' [{ FilePath }] form is taken as an image
    // sequence and refused for an ordinary file: "Invalid 'StartIndex' or
    // 'EndIndex' input specified" (Resolve 21.1.0).
    const imported = await mediaPool.ImportMedia([filePath]);
    if (!imported || imported.length === 0) throw new Error("Resolve couldn't import the file.");
    return { clip: imported[0], bin, added: true };
}

async function addToBin(resolve, filePath, realpath = fs.realpathSync.native) {
    const project = await currentProject(resolve);
    const { bin, added } = await clipInCurrentBin(await project.GetMediaPool(), filePath, realpath);
    const name = await bin.GetName();
    return added ? `Added to ${name}.` : `Already in ${name}.`;
}

// Each track's items as spans. A locked track counts as full: Resolve refuses
// to place a clip there.
async function trackSpans(timeline, type) {
    const count = (await timeline.GetTrackCount(type)) || 0;
    const tracks = [];
    for (let index = 1; index <= count; index += 1) {
        const spans = [];
        if (await timeline.GetIsTrackLocked(type, index)) {
            spans.push({ start: -Infinity, end: Infinity });
        }
        for (const item of (await timeline.GetItemListInTrack(type, index)) || []) {
            spans.push({ start: await item.GetStart(), end: await item.GetEnd() });
        }
        tracks.push(spans);
    }
    return tracks;
}

async function itemStartingAt(timeline, type, index, frame) {
    for (const item of (await timeline.GetItemListInTrack(type, index)) || []) {
        if (Math.abs((await item.GetStart()) - frame) < 1) return item;
    }
    return null;
}

// Puts the file on the current timeline at the playhead, on the first track
// free there (adding one when none is). Stills take the project's still
// length, which the clip doesn't say, so they need a track clear from the
// playhead on.
async function addAtPlayhead(resolve, filePath, mediaType, realpath = fs.realpathSync.native) {
    const project = await currentProject(resolve);
    const timeline = await project.GetCurrentTimeline();
    if (!timeline) throw new Error('Open a timeline first.');
    const mediaPool = await project.GetMediaPool();
    const { clip } = await clipInCurrentBin(mediaPool, filePath, realpath);

    const settings = (await timeline.GetSettings()) || {};
    const fps = Number(settings.timelineFrameRate);
    const timecode = await timeline.GetCurrentTimecode();
    const dropFrame = timecode.includes(';') || String(settings.timelineDropFrameTimecode) === '1';
    const start = timecodeToFrame(timecode, fps, dropFrame);

    const properties = (await clip.GetClipProperty()) || {};
    const isVideo = mediaType === 'video';
    const duration = isVideo ? timelineFrames(Number(properties.Frames), Number(properties.FPS), fps) : Infinity;
    const channels = properties['Audio Ch'];
    const needsAudio = isVideo && (channels === undefined || channels === '' || Number(channels) > 0);

    const video = await trackSpans(timeline, 'video');
    const audio = needsAudio ? await trackSpans(timeline, 'audio') : [];
    const track = pickTrack({ video, audio, start, duration, needsAudio });
    for (let count = video.length; count < track; count += 1) await timeline.AddTrack('video');
    for (let count = audio.length; needsAudio && count < track; count += 1) await timeline.AddTrack('audio', 'stereo');

    await mediaPool.AppendToTimeline([{ mediaPoolItem: clip, recordFrame: start, trackIndex: track }]);
    // What AppendToTimeline returns isn't proof: it drops a clip it can't
    // place without saying so. Read the track back.
    if (!(await itemStartingAt(timeline, 'video', track, start))) {
        throw new Error(`Resolve didn't place the clip on V${track}.`);
    }
    return `Added at ${timecode} on V${track}.`;
}

module.exports = {
    addAtPlayhead,
    addToBin,
    currentProjectName,
    pickTrack,
    projectFilePaths,
    timecodeToFrame,
    timelineFrames,
};
