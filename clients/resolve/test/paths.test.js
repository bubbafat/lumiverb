const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const { onDisk, pathKey } = require('../plugin/paths');

const missing = () => {
    throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
};

test('onDisk is the real path in NFC, or null when there is no file', () => {
    assert.equal(onDisk('/Volumes/media-01/a.mov', (p) => p.replace('media-01', 'MEDIA-01')), '/Volumes/MEDIA-01/a.mov');
    assert.equal(onDisk('/x/Café.mov', (p) => p), '/x/Café.mov');
    assert.equal(onDisk('/x/gone.mov', missing), null);
    assert.equal(onDisk(null, (p) => p), null);
});

test('pathKey falls back to the path as given, in NFC, for media that is offline', () => {
    assert.equal(pathKey('/Volumes/OFFLINE/Café.mov', missing), '/Volumes/OFFLINE/Café.mov');
    assert.equal(pathKey('/Volumes/media-01/a.mov', (p) => p.replace('media-01', 'MEDIA-01')), '/Volumes/MEDIA-01/a.mov');
});

// The fix rests on the native realpath returning the disk's spelling; check
// it on a real file. Only meaningful where the file system ignores case
// (the Mac's default).
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lumiverb-paths-'));
const file = path.join(dir, 'IMG_8223.MOV');
fs.writeFileSync(file, '');
const wrongCase = path.join(dir, 'img_8223.mov');
const ignoresCase = fs.existsSync(wrongCase);

test('the real realpath spells a file the way the disk does', { skip: !ignoresCase && 'case-sensitive file system' }, () => {
    assert.equal(onDisk(wrongCase), fs.realpathSync.native(file));
    assert.equal(path.basename(onDisk(wrongCase)), 'IMG_8223.MOV');
    assert.equal(pathKey(wrongCase), pathKey(file));
});
