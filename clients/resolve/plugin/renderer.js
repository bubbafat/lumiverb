// The plugin's page: sign in, search, show 10 clips at a time, and add one to
// the bin or the timeline. Everything goes through window.lumiverb (preload.js).

const $ = (id) => document.getElementById(id);

// Cursors of the pages seen so far: cursors[i] is page i's `after`.
let cursors = [null];
let pageIndex = 0;
let nextCursor = null;
let query = '';
let ticket = 0;

async function call(method, ...args) {
    const result = await window.lumiverb[method](...args);
    if (result.ok) return result.value;
    if (result.signedOut) showSignIn(result.error);
    throw new Error(result.error);
}

function showSignIn(message) {
    $('search').hidden = true;
    $('signin').hidden = false;
    $('signin-status').textContent = message || '';
    $('password').value = '';
    ($('server').value ? ($('email').value ? $('password') : $('email')) : $('server')).focus();
}

function showSearch(email) {
    $('signin').hidden = true;
    $('search').hidden = false;
    $('who').textContent = email;
    $('query').focus();
}

function duration(seconds) {
    if (seconds == null) return '';
    const total = Math.round(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = String(total % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
}

async function add(method, clip, buttons) {
    buttons.forEach((button) => { button.disabled = true; });
    $('status').textContent = `Adding ${clip.name}…`;
    try {
        $('status').textContent = await call(method, clip.assetId);
    } catch (error) {
        $('status').textContent = error.message;
    } finally {
        buttons.forEach((button) => { button.disabled = false; });
    }
}

function row(clip) {
    const item = element('li', 'clip');
    const thumb = element('img', 'thumb');
    thumb.alt = '';
    item.append(thumb);
    call('thumbnail', clip.assetId)
        .then((url) => { if (url) thumb.src = url; })
        .catch(() => {});

    const info = element('div', 'info');
    info.append(element('div', 'name', clip.name));
    const meta = [clip.folder !== '.' ? clip.folder : '', clip.libraryName, duration(clip.durationSec)];
    info.append(element('div', 'meta', meta.filter(Boolean).join(' · ')));
    if (clip.snippet) info.append(element('div', 'snippet', clip.snippet));

    if (clip.onThisMac) {
        const actions = element('div', 'actions');
        const toBin = element('button', null, 'Add to bin');
        const atPlayhead = element('button', null, 'Add at playhead');
        const buttons = [toBin, atPlayhead];
        toBin.type = 'button';
        atPlayhead.type = 'button';
        toBin.addEventListener('click', () => add('addToBin', clip, buttons));
        atPlayhead.addEventListener('click', () => add('addAtPlayhead', clip, buttons));
        actions.append(toBin, atPlayhead);
        info.append(actions);
    } else {
        info.append(element('div', 'missing', 'Not on this Mac'));
    }
    item.append(info);
    return item;
}

async function loadPage() {
    const mine = ++ticket;
    $('status').textContent = 'Searching…';
    $('prev').disabled = true;
    $('next').disabled = true;
    try {
        const page = await call('search', query, cursors[pageIndex]);
        if (mine !== ticket) return;
        nextCursor = page.nextCursor;
        $('results').replaceChildren(...page.items.map(row));
        const about = page.totalEstimate ? ` · about ${page.totalEstimate} results` : '';
        $('page').textContent = `Page ${pageIndex + 1}${about}`;
        $('pager').hidden = page.items.length === 0 && pageIndex === 0;
        $('prev').disabled = pageIndex === 0;
        $('next').disabled = !nextCursor;
        $('status').textContent = page.items.length ? '' : 'No results.';
    } catch (error) {
        if (mine === ticket) $('status').textContent = error.message;
    }
}

$('signin-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('signin-button').disabled = true;
    $('signin-status').textContent = 'Signing in…';
    try {
        await call('signIn', $('server').value, $('email').value, $('password').value);
        $('password').value = '';
        showSearch($('email').value);
    } catch (error) {
        $('signin-status').textContent = error.message;
    } finally {
        $('signin-button').disabled = false;
    }
});

$('signout').addEventListener('click', async () => {
    await call('signOut').catch(() => {});
    $('results').replaceChildren();
    $('pager').hidden = true;
    showSignIn('');
});

$('search-form').addEventListener('submit', (event) => {
    event.preventDefault();
    query = $('query').value.trim();
    if (!query) return;
    cursors = [null];
    pageIndex = 0;
    loadPage();
});

$('next').addEventListener('click', () => {
    if (!nextCursor) return;
    cursors[pageIndex + 1] = nextCursor;
    pageIndex += 1;
    loadPage();
});

$('prev').addEventListener('click', () => {
    if (pageIndex === 0) return;
    pageIndex -= 1;
    loadPage();
});

(async () => {
    const session = await call('session');
    $('server').value = session.serverUrl;
    $('email').value = session.email;
    if (session.signedIn) showSearch(session.email);
    else showSignIn('');
})();
