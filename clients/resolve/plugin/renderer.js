// The plugin's page: sign in, search (all of Lumiverb, in a project, or not in
// it), show 10 clips at a time, and add one to the bin or the timeline, and to
// the project. Everything goes through window.lumiverb (preload.js).

const $ = (id) => document.getElementById(id);
const NEW_FROM_RESOLVE = 'new-from-resolve';

// Cursors of the pages seen so far: cursors[i] is page i's `after`.
let cursors = [null];
let pageIndex = 0;
let nextCursor = null;
let query = '';
let ticket = 0;
let projects = [];
let shownProject = '';

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
    loadProjects();
}

// Work in progress, with a spinner; nothing when `text` is empty.
function busy(text) {
    $('busy').hidden = !text;
    $('busy-text').textContent = text || '';
}

function selectedProject() {
    return projects.find((project) => project.projectId === $('project').value) || null;
}

function scope() {
    return document.querySelector('input[name="scope"]:checked').value;
}

function setScope(value) {
    document.querySelector(`input[name="scope"][value="${value}"]`).checked = true;
}

// In / Not in need a project; adding to one needs it to be yours.
function syncControls() {
    const project = selectedProject();
    for (const input of document.querySelectorAll('input[name="scope"]')) {
        input.disabled = input.value !== 'all' && !project;
    }
    if (!project) setScope('all');
    $('also-add').disabled = !project || !project.own;
    $('also-add-label').title = !project ? 'Choose a project first.'
        : project.own ? '' : 'Only its owner can add clips to a shared project.';
}

function renderProjects(selected) {
    const options = [new Option('No project', '')];
    for (const project of projects) {
        options.push(new Option(project.own ? project.name : `${project.name} (shared)`, project.projectId));
    }
    options.push(new Option('New from Resolve project…', NEW_FROM_RESOLVE));
    $('project').replaceChildren(...options);
    $('project').value = selected || '';
    shownProject = $('project').value;
    syncControls();
}

// Your projects. The one named like the open Resolve project (the newest, if
// several) is picked, searching what isn't in it.
async function loadProjects() {
    try {
        const { projects: list, resolveProject } = await call('projects');
        projects = list;
        const match = list
            .filter((project) => resolveProject && project.name === resolveProject)
            .sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || ''))[0];
        renderProjects(match ? match.projectId : '');
        if (match) setScope('out');
    } catch (error) {
        projects = [];
        renderProjects('');
        $('status').textContent = `Projects: ${error.message}`;
    }
}

async function newFromResolve() {
    $('project').disabled = true;
    busy('Reading the Media Pool…');
    const stop = window.lumiverb.onProgress(({ step, done, total }) => {
        busy(step === 'reading' ? `Reading the Media Pool… ${done}` : `Matching clips… ${done} of ${total}`);
    });
    try {
        const { project, clips, notInLumiverb } = await call('newFromResolve');
        projects = [project, ...projects];
        renderProjects(project.projectId);
        setScope('out');
        await refresh();
        const missing = notInLumiverb.length ? ` ${notInLumiverb.length} not in Lumiverb.` : '';
        $('status').textContent = `Made ${project.name}: ${clips} clips.${missing}`;
    } catch (error) {
        $('project').value = shownProject;
        syncControls();
        $('status').textContent = error.message;
    } finally {
        stop();
        busy('');
        $('project').disabled = false;
    }
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

// The project an add also goes to: the chosen one, when it's yours and the
// box is checked.
function addTarget() {
    const project = selectedProject();
    return project && project.own && $('also-add').checked ? project.projectId : null;
}

async function add(method, clip, buttons) {
    buttons.forEach((button) => { button.disabled = true; });
    busy(`Adding ${clip.name}…`);
    try {
        $('status').textContent = await call(method, clip.assetId, addTarget());
    } catch (error) {
        $('status').textContent = error.message;
    } finally {
        busy('');
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
    busy('Searching…');
    $('prev').disabled = true;
    $('next').disabled = true;
    try {
        const project = selectedProject();
        const page = await call('search', {
            text: query,
            media: $('media').value,
            scope: scope(),
            project: project ? project.projectId : null,
            after: cursors[pageIndex],
        });
        if (mine !== ticket) return;
        nextCursor = page.nextCursor;
        $('results').replaceChildren(...page.items.map(row));
        $('page').textContent = `Page ${pageIndex + 1}`;
        $('pager').hidden = page.items.length === 0 && pageIndex === 0 && !nextCursor;
        $('prev').disabled = pageIndex === 0;
        $('next').disabled = !nextCursor;
        $('status').textContent = page.items.length ? '' : 'No results.';
    } catch (error) {
        if (mine === ticket) $('status').textContent = error.message;
    } finally {
        if (mine === ticket) busy('');
    }
}

// The filters changed (project, scope, type): the same place in the results
// with the new ones. A cursor is a place in the search's order whatever the
// filters, so the page you're on stays where it was.
async function refresh() {
    if (query) await loadPage();
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
    projects = [];
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

$('project').addEventListener('change', () => {
    if ($('project').value === NEW_FROM_RESOLVE) {
        newFromResolve();
        return;
    }
    // Choosing a project searches what isn't in it yet, unless a scope was chosen.
    if (!shownProject && selectedProject()) setScope('out');
    shownProject = $('project').value;
    syncControls();
    refresh();
});

for (const input of document.querySelectorAll('input[name="scope"]')) {
    input.addEventListener('change', refresh);
}
$('media').addEventListener('change', refresh);

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
