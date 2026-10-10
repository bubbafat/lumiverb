const assert = require('node:assert/strict');
const { test } = require('node:test');

const { addAtPlayhead, addToBin, pickTrack, projectFilePaths, timecodeToFrame, timelineFrames } = require('../plugin/resolve');

// A fake realpath: every file is there, spelled as given. Keeps the tests
// independent of what this machine has mounted.
const asGiven = (p) => p;

// A fake of the slice of Resolve's scripting API the plugin uses. Like
// Resolve, AppendToTimeline drops a clip that would land on another one.
function fakeClip(filePath, properties = {}) {
    return { GetClipProperty: async () => ({ 'File Path': filePath, Frames: '120', FPS: 24, 'Audio Ch': '2', ...properties }) };
}

// A Media Pool bin: { name, clips: [paths], bins: [nested] }.
function fakeBin({ name = 'Master', clips = [], bins = [] } = {}) {
    return {
        GetName: async () => name,
        GetClipList: async () => clips.map((filePath) => fakeClip(filePath)),
        GetSubFolderList: async () => bins.map(fakeBin),
    };
}

function fakeResolve({ binClips = [], root = null, timeline = null } = {}) {
    const calls = { imported: [], appended: [], addedTracks: [] };
    const clip = fakeClip;
    const bin = fakeBin({ clips: binClips });
    const item = (start, end) => ({ GetStart: async () => start, GetEnd: async () => end });
    const tracks = timeline && { video: timeline.video.map((t) => t.map(([s, e]) => item(s, e))), audio: timeline.audio.map((t) => t.map(([s, e]) => item(s, e))) };
    const busy = (list, start, end) => list.some((it) => it.start < end && start < it.end);

    const fakeTimeline = timeline && {
        GetSettings: async () => ({ timelineFrameRate: timeline.fps, timelineDropFrameTimecode: timeline.dropFrame ? '1' : '0' }),
        GetCurrentTimecode: async () => timeline.timecode,
        GetTrackCount: async (type) => tracks[type].length,
        GetIsTrackLocked: async (type, index) => (timeline.locked || []).includes(`${type}${index}`),
        GetItemListInTrack: async (type, index) => tracks[type][index - 1] || [],
        AddTrack: async (type, subType) => {
            calls.addedTracks.push(subType ? `${type}:${subType}` : type);
            tracks[type].push([]);
            return true;
        },
    };

    const mediaPool = {
        GetRootFolder: async () => fakeBin(root || {}),
        GetCurrentFolder: async () => bin,
        // Like Resolve 21.1.0: an object is an image sequence, refused
        // without StartIndex and EndIndex; a path string imports the file.
        ImportMedia: async (items) => {
            for (const item of items) {
                if (typeof item !== 'string' && (item.StartIndex === undefined || item.EndIndex === undefined)) {
                    throw new Error("Execute: ImportMedia: Error response (errorStr:Invalid 'StartIndex' or 'EndIndex' input specified.) (errorCode:2)");
                }
            }
            calls.imported.push(...items);
            return items.map((filePath) => clip(filePath, timeline && timeline.clip));
        },
        AppendToTimeline: async (infos) => {
            calls.appended.push(...infos);
            const placed = [];
            for (const info of infos) {
                const props = await info.mediaPoolItem.GetClipProperty();
                const start = info.recordFrame;
                const end = start + Number(props.Frames);
                const track = tracks.video[info.trackIndex - 1];
                const spans = await Promise.all(track.map(async (it) => ({ start: await it.GetStart(), end: await it.GetEnd() })));
                if (timeline.dropEverything || busy(spans, start, end)) continue;
                track.push(item(start, end));
                placed.push({});
            }
            return placed;
        },
    };
    const project = { GetMediaPool: async () => mediaPool, GetCurrentTimeline: async () => fakeTimeline };
    const resolve = { GetProjectManager: async () => ({ GetCurrentProject: async () => project }) };
    return { resolve, calls, tracks, clip };
}

test('timecodeToFrame counts frames the way Resolve numbers them', () => {
    assert.equal(timecodeToFrame('01:00:00:00', 24), 86400);
    assert.equal(timecodeToFrame('01:00:10:12', 23.976), 86400 + 240 + 12);
    assert.equal(timecodeToFrame('00:00:01:05', 25), 30);
    // Drop-frame: 00:01:00;02 is the first frame of minute 1, frame 1800.
    assert.equal(timecodeToFrame('00:01:00;02', 29.97, true), 1800);
    assert.equal(timecodeToFrame('00:10:00;00', 29.97, true), 17982);
    assert.equal(timecodeToFrame('01:00:00;00', 29.97, true), 107892);
    assert.equal(timecodeToFrame('00:01:00;04', 59.94, true), 3600);
    // Non-drop 29.97 counts every frame.
    assert.equal(timecodeToFrame('00:01:00:00', 29.97, false), 1800);
    assert.throws(() => timecodeToFrame('soon', 24), /Can't read the playhead timecode/);
});

test('timelineFrames floors source frames into timeline frames', () => {
    assert.equal(timelineFrames(240, 24, 24), 240);
    assert.equal(timelineFrames(250, 25, 24), 240);
    assert.equal(timelineFrames(NaN, 24, 24), Infinity);
    assert.equal(timelineFrames(100, 0, 24), Infinity);
});

test('pickTrack takes the first track free for the whole clip', () => {
    const v1Busy = [[{ start: 0, end: 100 }]];
    assert.equal(pickTrack({ video: [[]], start: 50, duration: 10 }), 1);
    assert.equal(pickTrack({ video: v1Busy, start: 50, duration: 10 }), 2, 'V1 busy at the playhead: a new V2');
    assert.equal(pickTrack({ video: v1Busy, start: 100, duration: 10 }), 1, 'a clip ending at the playhead is not in the way');
    assert.equal(pickTrack({ video: [[{ start: 120, end: 130 }]], start: 100, duration: 30 }), 2, 'one starting before the clip ends is');
    assert.equal(pickTrack({ video: [[], []], audio: [[{ start: 0, end: 100 }]], start: 50, duration: 10, needsAudio: true }), 2, 'A1 busy');
    assert.equal(pickTrack({ video: [[]], audio: [[{ start: 0, end: 100 }]], start: 50, duration: 10 }), 1, 'a silent clip ignores audio');
    assert.equal(pickTrack({ video: [[{ start: 900, end: 950 }], []], start: 50, duration: Infinity }), 2, 'a still needs the track clear from the playhead on');
});

test('addToBin imports the original into the current bin', async () => {
    const { resolve, calls } = fakeResolve();
    assert.equal(await addToBin(resolve, '/Volumes/media-01/Media/a.mov', asGiven), 'Added to Master.');
    assert.deepEqual(calls.imported, ['/Volumes/media-01/Media/a.mov'], 'a path string: Resolve refuses { FilePath } for a file');
});

test("addToBin doesn't import a file the bin already has", async () => {
    const { resolve, calls } = fakeResolve({ binClips: ['/Volumes/media-01/Media/a.mov'] });
    assert.equal(await addToBin(resolve, '/Volumes/media-01/Media/a.mov', asGiven), 'Already in Master.');
    assert.deepEqual(calls.imported, []);
});

test('addToBin matches a file the bin has under the other Unicode form', async () => {
    const { resolve, calls } = fakeResolve({ binClips: ['/Volumes/media-01/Media/Café.mov'] });
    assert.equal(await addToBin(resolve, '/Volumes/media-01/Media/Café.mov', asGiven), 'Already in Master.');
    assert.deepEqual(calls.imported, []);
});

test('a clip the bin has is found however the volume is spelled', async () => {
    // Resolve stores the mount's spelling; Lumiverb's root may differ in case.
    const disk = (p) => p.replace('/Volumes/media-01/', '/Volumes/MEDIA-01/');
    const { resolve, calls } = fakeResolve({ binClips: ['/Volumes/MEDIA-01/Media/IMG_8223.MOV'] });
    assert.equal(await addToBin(resolve, '/Volumes/media-01/Media/IMG_8223.MOV', disk), 'Already in Master.');
    assert.deepEqual(calls.imported, []);
});

test("projectFilePaths keys each clip by the disk's spelling, and offline ones as stored", async () => {
    const disk = (p) => {
        if (p.startsWith('/Volumes/OFFLINE/')) throw new Error('ENOENT');
        return p.replace('/Volumes/media-01/', '/Volumes/MEDIA-01/');
    };
    const { resolve } = fakeResolve({
        root: { clips: ['/Volumes/media-01/Media/a.mov', '/Volumes/OFFLINE/Café.mov'] },
    });

    const paths = await projectFilePaths(resolve, disk);

    assert.deepEqual([...paths].sort(), ['/Volumes/MEDIA-01/Media/a.mov', '/Volumes/OFFLINE/Café.mov']);
});

test('projectFilePaths lists every clip in every bin, in NFC', async () => {
    const { resolve } = fakeResolve({
        root: {
            name: 'Master',
            clips: ['/Volumes/media-01/Media/a.mov'],
            bins: [
                { name: 'Selects', clips: ['/Volumes/media-01/Media/b.mov'], bins: [{ name: 'Deep', clips: ['/Volumes/media-01/Media/Café.mov'] }] },
                { name: 'Music', clips: ['/Users/robert/Music/track.wav'] },
            ],
        },
    });

    const paths = await projectFilePaths(resolve, asGiven);

    assert.deepEqual([...paths].sort(), [
        '/Users/robert/Music/track.wav',
        '/Volumes/media-01/Media/Café.mov',
        '/Volumes/media-01/Media/a.mov',
        '/Volumes/media-01/Media/b.mov',
    ]);
});

test('addAtPlayhead puts the clip at the playhead on the first free track', async () => {
    const { resolve, calls, tracks } = fakeResolve({
        timeline: { fps: 24, timecode: '01:00:10:00', video: [[[86400, 87000]]], audio: [[[86400, 87000]]] },
    });

    const message = await addAtPlayhead(resolve, '/Volumes/media-01/Media/a.mov', 'video', asGiven);

    assert.equal(message, 'Added at 01:00:10:00 on V2.');
    assert.deepEqual(calls.addedTracks, ['video', 'audio:stereo']);
    assert.equal(calls.appended.length, 1);
    assert.equal(calls.appended[0].recordFrame, 86640);
    assert.equal(calls.appended[0].trackIndex, 2);
    assert.equal(tracks.video[0].length, 1, 'the clip already on V1 is untouched');
});

test('addAtPlayhead uses a free existing track without adding one', async () => {
    const { resolve, calls } = fakeResolve({
        timeline: { fps: 24, timecode: '01:00:10:00', video: [[[86400, 87000]], []], audio: [[[86400, 87000]], []] },
    });
    assert.equal(await addAtPlayhead(resolve, '/x/a.mov', 'video', asGiven), 'Added at 01:00:10:00 on V2.');
    assert.deepEqual(calls.addedTracks, []);
});

test('addAtPlayhead skips a locked track', async () => {
    const { resolve, calls } = fakeResolve({
        timeline: { fps: 24, timecode: '01:00:00:00', video: [[], []], audio: [[], []], locked: ['video1'] },
    });
    await addAtPlayhead(resolve, '/x/a.mov', 'video', asGiven);
    assert.equal(calls.appended[0].trackIndex, 2);
});

test('addAtPlayhead reports a clip Resolve dropped', async () => {
    const { resolve } = fakeResolve({
        timeline: { fps: 24, timecode: '01:00:00:00', video: [[]], audio: [[]], dropEverything: true },
    });
    await assert.rejects(addAtPlayhead(resolve, '/x/a.mov', 'video', asGiven), /Resolve didn't place the clip on V1\./);
});

test('addAtPlayhead needs an open timeline', async () => {
    const { resolve } = fakeResolve();
    await assert.rejects(addAtPlayhead(resolve, '/x/a.mov', 'video', asGiven), /Open a timeline first\./);
});
