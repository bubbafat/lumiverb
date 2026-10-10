// One spelling per file, so a path from Lumiverb and one from Resolve compare
// equal when they name the same file. A library root can be spelled
// differently from the mount it names (`/Volumes/media-01` for a disk
// mounted as `/Volumes/MEDIA-01`: the same folder to a case-insensitive Mac),
// so comparing the strings isn't enough.

const fs = require('fs');

// The file's real path as the disk spells it (its case, symlinks resolved),
// in NFC; null when there's no file there. Node's plain realpathSync keeps
// the case it was given; the native one returns the disk's.
function onDisk(filePath, realpath = fs.realpathSync.native) {
    if (!filePath) return null;
    try {
        return realpath(filePath).normalize('NFC');
    } catch {
        return null;
    }
}

// What a path is compared by: its real path when the file is there, else the
// path as given (media that's offline), in NFC.
function pathKey(filePath, realpath = fs.realpathSync.native) {
    return onDisk(filePath, realpath) || filePath.normalize('NFC');
}

module.exports = { onDisk, pathKey };
