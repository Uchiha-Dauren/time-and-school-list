'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'];
const EVENT_LABELS = {lesson_start: 'Lesson start', lesson_end: 'Lesson end', break_start: 'Break start', long_break_start: 'Long break start', end_of_day: 'End of school day'};
const PAGE_LABELS = {overview: 'Overview', school: 'School data', timetable: 'Timetable', music: 'Music library', bells: 'Bells & settings'};
let catalog = null;
let timetable = {lessons: [], periods: []};
let music = {tracks: [], playlists: {}, event_types: []};
let bells = {events: [], periods: [], priority: []};
let playlistDraft = [];
let editingTeacher = null;
let editingClass = null;
let editingLesson = null;
let generating = false;
let playerBusy = false;
let playerPollPending = false;
let lastPlayerError = null;

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function notify(message) {
  $('#error').hidden = true;
  const box = $('#notice');
  box.textContent = message;
  box.hidden = false;
}

function showError(error) {
  $('#notice').hidden = true;
  const box = $('#error');
  box.textContent = error instanceof Error ? error.message : String(error);
  box.hidden = false;
  box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

async function api(path, options = {}) {
  const init = {...options};
  if (init.body && !(init.body instanceof FormData)) {
    init.headers = {'Content-Type': 'application/json', ...init.headers};
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    const detail = data.detail || data.message || response.statusText;
    let message;
    if (Array.isArray(detail)) {
      message = detail.map(item => `${(item.loc || []).slice(1).join('.')}: ${item.msg || JSON.stringify(item)}`).join('\n');
    } else if (typeof detail === 'object') {
      message = [detail.message, ...(detail.details || [])].filter(Boolean).join('\n');
    } else message = String(detail);
    throw new Error(message || `Request failed (${response.status}).`);
  }
  return data;
}

function guard(handler) {
  return async event => {
    try { await handler(event); } catch (error) { showError(error); }
  };
}

async function pending(button, action, pendingText = 'Saving…') {
  if (!button || button.disabled) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = pendingText;
  try { return await action(); }
  finally { button.disabled = false; button.textContent = original; }
}

function showPage(name) {
  if (!PAGE_LABELS[name]) name = 'overview';
  $$('.page').forEach(page => { page.hidden = page.id !== `page-${name}`; });
  $$('.nav-item').forEach(button => {
    const active = button.dataset.page === name;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  $('#page-name').textContent = PAGE_LABELS[name];
  history.replaceState(null, '', `#${name}`);
}

function table(container, headers, rows, empty = 'Nothing here yet.') {
  const root = typeof container === 'string' ? $(container) : container;
  root.replaceChildren();
  if (!rows.length) { root.append(el('div', empty, 'empty-state')); return; }
  const node = el('table');
  const head = el('thead');
  const headRow = el('tr');
  headers.forEach(title => headRow.append(el('th', title)));
  head.append(headRow);
  const body = el('tbody');
  rows.forEach(values => {
    const row = el('tr');
    values.forEach(value => {
      const cell = el('td');
      if (value instanceof Node) cell.append(value); else cell.textContent = String(value ?? '—');
      row.append(cell);
    });
    body.append(row);
  });
  node.append(head, body);
  root.append(node);
}

function options(select, items, label, placeholder = 'Choose…') {
  const previous = select.value;
  const selected = select.multiple ? [...select.selectedOptions].map(x => x.value) : [];
  select.replaceChildren();
  if (!select.multiple && placeholder !== null) {
    const opt = el('option', placeholder);
    opt.value = '';
    select.append(opt);
  }
  items.forEach(item => {
    const opt = el('option', typeof label === 'function' ? label(item) : item[label]);
    opt.value = String(item.id);
    if (select.multiple) opt.selected = selected.includes(opt.value);
    select.append(opt);
  });
  if (!select.multiple && [...select.options].some(x => x.value === previous)) select.value = previous;
}

function lookup(items, id, fallback = '—') { return items.find(item => item.id === id)?.name || fallback; }
function gradeName(id) { const grade = catalog.grades.find(item => item.id === id); return grade ? `Grade ${grade.number}` : '—'; }
function classLabel(item) { return `${item.name} · ${item.language === 'ru' ? 'РУС' : 'ҚАЗ'} · ${item.shift || 1}-ауысым`; }
function idValue(form, name) { return Number(form.elements[name].value); }
function canInvalidate() {
  return !timetable.lessons.length || window.confirm('This school-data change clears the generated timetable and bell schedule. Continue?');
}

async function refresh() {
  const results = await Promise.all([api('/api/catalog'), api('/api/timetable'), api('/api/music'), api('/api/bells')]);
  [catalog, timetable, music, bells] = results;
  renderCatalog();
  renderOverview();
  renderMusic();
  renderBells();
  renderTimetable();
}

function renderOverview() {
  const items = [
    [catalog.classes.length, 'Classes in the school', '▦'],
    [catalog.teachers.length, 'Registered teachers', '◉'],
    [timetable.lessons.length, 'Scheduled weekly lessons', '▤'],
    [music.tracks.length, 'Music tracks', '♫']
  ];
  $('#stats').replaceChildren(...items.map(([count, label, symbol]) => {
    const node = el('div', null, 'stat');
    node.append(el('small', symbol), el('strong', count), el('span', label));
    return node;
  }));
  $('#hero-start').textContent = catalog.settings.start_time;
  $('#overview-summary').textContent = timetable.lessons.length ? `${timetable.lessons.length} lessons scheduled across ${catalog.classes.length} classes. Open Timetable to inspect the week.` : 'No timetable yet. Finish school data, then generate your first week.';
  $('#seed-button').disabled = catalog.teachers.length > 0 || catalog.grades.length > 0 || catalog.subjects.length > 0;
  const scheduler = $('#scheduler-chip');
  scheduler.textContent = catalog.settings.scheduler_enabled ? 'Scheduler on' : 'Scheduler off';
  scheduler.classList.toggle('on', catalog.settings.scheduler_enabled);
  const audio = $('#audio-chip');
  audio.textContent = catalog.settings.audio_enabled ? 'Speaker output enabled' : 'Audio simulation';
  audio.classList.toggle('on', catalog.settings.audio_enabled);
  renderReadiness();
}

function renderReadiness() {
  const missing = [];
  for (const schoolClass of catalog.classes) {
    const requirements = catalog.grade_subjects.filter(item => item.grade_id === schoolClass.grade_id);
    const assigned = catalog.assignments.filter(item => item.class_id === schoolClass.id);
    if (!requirements.length && !assigned.some(item => item.lessons_per_week)) missing.push(`${schoolClass.name}: апталық оқу жоспары енгізілмеген.`);
    for (const requirement of requirements) {
      if (!catalog.assignments.some(item => item.class_id === schoolClass.id && item.subject_id === requirement.subject_id)) {
        missing.push(`${schoolClass.name} · ${lookup(catalog.subjects, requirement.subject_id)}: мұғалім тағайындалмаған.`);
      }
    }
  }
  const container = $('#setup-check');
  container.replaceChildren();
  if (!catalog.classes.length) {
    container.append(el('p', 'Алдымен сыныптар мен пәндерді қосыңыз немесе демо деректерін жүктеңіз.', 'hint'));
  } else if (missing.length) {
    container.append(el('strong', `Толықтыру керек: ${missing.length}`));
    const list = el('ul');
    missing.slice(0, 12).forEach(message => list.append(el('li', message)));
    container.append(list);
    if (missing.length > 12) container.append(el('p', `Тағы ${missing.length - 12} тағайындау немесе оқу жоспары қажет.`, 'hint'));
  } else {
    container.append(el('strong', 'Оқу жоспары мен мұғалімдер толтырылған.'));
    container.append(el('p', 'Кестені құру кезінде мұғалімдер, уақыт және кабинет шектеулері қосымша тексеріледі.', 'hint'));
  }
}

function selectedLessons() {
  const isTeacher = $('#view-type').value === 'teacher';
  const selectedId = Number($('#view-id').value);
  return timetable.lessons.filter(lesson => (isTeacher ? lesson.teacher_id : lesson.class_id) === selectedId);
}

function timetableTitle() {
  const select = $('#view-id');
  return `Сабақ кестесі · ${select.selectedOptions[0]?.textContent || ''}`;
}

function exportTimetable() {
  const lessons = selectedLessons();
  if (!lessons.length) return;
  const rows = [['Күн', 'Ауысым', 'Сабақ', 'Басталуы', 'Аяқталуы', 'Сынып', 'Пән', 'Мұғалім', 'Кабинет']];
  lessons.sort((a, b) => a.weekday - b.weekday || (a.shift || 1) - (b.shift || 1) || a.period - b.period);
  lessons.forEach(lesson => {
    const period = lessonPeriods(lesson.shift || 1).find(item => item.period === lesson.period);
    rows.push([DAYS[lesson.weekday], lesson.shift || 1, lesson.period, period?.start || '', period?.end || '', lesson.class_name, lesson.subject_name, lesson.teacher_name, lesson.room_name || '']);
  });
  // Quote every field and neutralize spreadsheet formulas from user-entered names.
  const quote = value => {
    let text = String(value ?? '');
    if (/^[\s]*[=+@-]/.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  const csv = '\uFEFF' + rows.map(row => row.map(quote).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8;'}));
  const link = el('a');
  link.href = url;
  link.download = `timetable-${$('#view-type').value}-${$('#view-id').value}.csv`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function lessonPeriods(shift = 1) {
  return timetable.shift_periods?.[String(shift)] || (shift === 1 ? timetable.periods : []);
}

function renderCatalog() {
  const c = catalog;
  options($('#teacher-form [name=subject_ids]'), c.subjects, 'name', null);
  for (const selector of ['#class-form [name=grade_id]', '#grade-limit-form [name=grade_id]', '#curriculum-form [name=grade_id]']) {
    options($(selector), c.grades, item => `Grade ${item.number}`);
  }
  options($('#curriculum-form [name=subject_id]'), c.subjects, 'name');
  options($('#assignment-form [name=class_id]'), c.classes, classLabel);
  options($('#assignment-form [name=room_id]'), c.rooms, 'name', 'No room');
  updateAssignmentChoices();
  table('#subject-list', ['Subject', 'Early preference'], c.subjects.map(s => [s.name, s.is_hard ? 'Yes' : '—']), 'Add your first subject.');
  table('#teacher-list', ['Teacher', 'Subjects', ''], c.teachers.map(teacher => {
    const edit = el('button', 'Edit', 'text-button');
    edit.type = 'button';
    edit.addEventListener('click', () => editTeacher(teacher));
    return [teacher.name, teacher.subject_ids.map(id => lookup(c.subjects, id)).join(', '), edit];
  }), 'Register teachers and their subject skills.');
  table('#grade-list', ['Grade', 'Classes', 'Daily limit'], c.grades.map(g => [`Grade ${g.number}`, c.classes.filter(x => x.grade_id === g.id).map(x => x.name).join(', '), g.max_lessons_per_day]), 'Add a grade to create its parallel classes.');
  table('#class-list', ['Сынып', ''], c.classes.map(schoolClass => {
    const edit = el('button', 'Edit', 'text-button');
    edit.type = 'button';
    edit.addEventListener('click', () => editClass(schoolClass));
    return [classLabel(schoolClass), edit];
  }), 'Мектептегі дайын сыныптарды атауы, оқу тілі және ауысымы бойынша енгізіңіз.');
  $('#room-list').replaceChildren(...c.rooms.map(x => el('span', x.name, 'tag')));
  table('#curriculum-list', ['Grade', 'Subject', 'Per week'], c.grade_subjects.map(g => [gradeName(g.grade_id), lookup(c.subjects, g.subject_id), g.lessons_per_week]), 'Define required weekly lessons.');
  table('#assignment-list', ['Class / subject', 'Teacher', 'Room', 'Аптасына'], c.assignments.map(a => {
    const label = el('div');
    label.append(el('strong', lookup(c.classes, a.class_id)), el('small', lookup(c.subjects, a.subject_id)));
    const schoolClass = c.classes.find(item => item.id === a.class_id);
    const inherited = c.grade_subjects.find(item => item.grade_id === schoolClass?.grade_id && item.subject_id === a.subject_id)?.lessons_per_week;
    return [label, lookup(c.teachers, a.teacher_id), lookup(c.rooms, a.room_id), a.lessons_per_week ?? inherited ?? '—'];
  }), 'Assign a teacher to every required class–subject pair.');
  const settingsForm = $('#settings-form');
  Object.entries(c.settings).forEach(([key, value]) => {
    const input = settingsForm.elements[key];
    if (!input) return;
    if (input.type === 'checkbox') input.checked = Boolean(value); else input.value = value;
  });
  updateViewChoices();
  for (const form of ['#grade-form', '#class-form']) {
    const secondShift = $(`${form} [name=shift] option[value="2"]`);
    secondShift.disabled = !c.settings.shifts_enabled;
    if (!c.settings.shifts_enabled) $(`${form} [name=shift]`).value = '1';
  }
}

function updateAssignmentChoices() {
  if (!catalog) return;
  const form = $('#assignment-form');
  const schoolClass = catalog.classes.find(c => c.id === idValue(form, 'class_id'));
  const allowed = schoolClass ? catalog.grade_subjects.filter(g => g.grade_id === schoolClass.grade_id).map(g => g.subject_id) : [];
  options(form.elements.subject_id, schoolClass ? catalog.subjects : [], subject => `${subject.name}${allowed.includes(subject.id) ? ' · оқу жоспары' : ''}`, schoolClass ? 'Choose a subject…' : 'Choose a class first…');
  updateTeacherChoices();
}

function updateTeacherChoices() {
  const form = $('#assignment-form');
  const subjectId = idValue(form, 'subject_id');
  options(form.elements.teacher_id, catalog.teachers.filter(t => t.subject_ids.includes(subjectId)), 'name', subjectId ? 'Choose qualified teacher…' : 'Choose a subject first…');
  const schoolClass = catalog.classes.find(item => item.id === idValue(form, 'class_id'));
  const assigned = catalog.assignments.find(item => item.class_id === schoolClass?.id && item.subject_id === subjectId);
  form.elements.teacher_id.value = assigned ? String(assigned.teacher_id) : '';
  form.elements.room_id.value = assigned?.room_id ? String(assigned.room_id) : '';
  form.elements.lessons_per_week.value = assigned?.lessons_per_week ?? '';
  form.elements.lessons_per_week.required = Boolean(subjectId && !catalog.grade_subjects.some(item => item.grade_id === schoolClass?.grade_id && item.subject_id === subjectId));
}

function editTeacher(teacher) {
  editingTeacher = teacher.id;
  const form = $('#teacher-form');
  form.elements.name.value = teacher.name;
  [...form.elements.subject_ids.options].forEach(opt => { opt.selected = teacher.subject_ids.includes(Number(opt.value)); });
  $('#teacher-save').textContent = 'Save teacher';
  $('#teacher-cancel').hidden = false;
  form.scrollIntoView({behavior: 'smooth', block: 'center'});
}

function cancelTeacherEdit() {
  editingTeacher = null;
  $('#teacher-form').reset();
  $('#teacher-save').textContent = 'Register teacher';
  $('#teacher-cancel').hidden = true;
}

function editClass(schoolClass) {
  editingClass = schoolClass.id;
  const form = $('#class-form');
  for (const name of ['name', 'grade_id', 'language', 'shift']) form.elements[name].value = schoolClass[name];
  $('#class-save').textContent = 'Сыныпты сақтау';
  $('#class-cancel').hidden = false;
  form.scrollIntoView({behavior: 'smooth', block: 'center'});
}

function cancelClassEdit() {
  editingClass = null;
  $('#class-form').reset();
  $('#class-save').textContent = 'Add class';
  $('#class-cancel').hidden = true;
}

function updateViewChoices() {
  const isTeacher = $('#view-type').value === 'teacher';
  $('#view-label').textContent = isTeacher ? 'Teacher' : 'Class';
  options($('#view-id'), isTeacher ? catalog.teachers : catalog.classes, isTeacher ? 'name' : classLabel, null);
}

function renderTimetable() {
  const isTeacher = $('#view-type').value === 'teacher';
  const selectedId = Number($('#view-id').value);
  const selected = selectedLessons();
  const schoolClass = catalog.classes.find(item => item.id === selectedId);
  const shifts = isTeacher ? [...new Set(selected.map(item => item.shift || 1))].sort() : [schoolClass?.shift || 1];
  $('#timetable-title').textContent = timetableTitle();
  $('#timetable-export').disabled = !selected.length;
  $('#timetable-print').disabled = !selected.length;
  const container = $('#timetable-grid');
  container.replaceChildren();
  $('#timetable-count').textContent = selected.length ? `${selected.length} lessons this week. Moving a lesson also updates the bell schedule.` : '';
  if (!timetable.lessons.length) { container.append(el('div', 'No timetable yet. Add school data, then generate a week.', 'empty-state')); return; }
  if (!selectedId) { container.append(el('div', 'Choose a class or teacher to view their timetable.', 'empty-state')); return; }
  if (!selected.length) { container.append(el('div', 'Бұл мұғалімге немесе сыныпқа сабақ тағайындалмаған.', 'empty-state')); return; }
  const node = el('table', null, 'timetable');
  const head = el('thead');
  const headRow = el('tr');
  ['Lesson', ...DAYS].forEach(day => headRow.append(el('th', day)));
  head.append(headRow);
  const body = el('tbody');
  shifts.forEach(shift => {
    const shiftRow = el('tr', null, 'shift-heading');
    const title = el('th', `${shift}-ауысым · ${shift === 1 ? 'Таңертең' : 'Түстен кейін'}`);
    title.colSpan = DAYS.length + 1;
    shiftRow.append(title);
    body.append(shiftRow);
    lessonPeriods(shift).forEach(period => {
      const row = el('tr');
      const time = el('td', null, 'period-cell');
      time.append(el('strong', `L${period.period}`), el('span', `${period.start}–${period.end}`));
      row.append(time);
      DAYS.forEach((_, weekday) => {
        const cell = el('td');
        const lesson = selected.find(item => item.weekday === weekday && item.period === period.period && (item.shift || 1) === shift);
        if (lesson) {
          const button = el('button', null, 'lesson-cell');
          button.type = 'button';
          button.setAttribute('aria-label', `Move ${lesson.subject_name}, ${DAYS[weekday]}, shift ${shift}, lesson ${period.period}`);
          button.append(el('strong', lesson.subject_name), el('small', isTeacher ? lesson.class_name : lesson.teacher_name));
          if (lesson.room_name) button.append(el('span', lesson.room_name, 'lesson-room'));
          button.addEventListener('click', () => editLesson(lesson));
          cell.append(button);
        } else { cell.className = 'free-cell'; cell.textContent = '·'; }
        row.append(cell);
      });
      body.append(row);
    });
  });
  node.append(head, body);
  container.append(node);
}

function editLesson(lesson) {
  editingLesson = lesson.id;
  $('#edit-lesson-title').textContent = `${lesson.class_name} · ${lesson.subject_name} · ${lesson.teacher_name}`;
  const form = $('#lesson-form');
  form.elements.weekday.value = lesson.weekday;
  options(form.elements.period, lessonPeriods(lesson.shift || 1).map(p => ({id: p.period, label: `L${p.period} · ${p.start}–${p.end}`})), 'label', null);
  form.elements.period.value = lesson.period;
  $('#lesson-editor').hidden = false;
  $('#lesson-editor').scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

async function generate() {
  if (generating) return;
  if (timetable.lessons.length && !window.confirm('Generate a new timetable? This replaces the current week and any manual moves.')) return;
  generating = true;
  const buttons = $$('[data-generate]');
  buttons.forEach(button => { button.disabled = true; button.textContent = 'Finding a timetable…'; });
  notify(`Generating a conflict-free week. The solver can take up to ${catalog.settings.solver_time_limit} seconds.`);
  try {
    const result = await api('/api/timetable/generate', {method: 'POST'});
    $('#lesson-editor').hidden = true;
    await refresh();
    $('#generation-info').textContent = result.seed !== undefined ? `Соңғы құру нөмірі (seed): ${result.seed}. Уақыт шектеуіне байланысты бірдей seed кезінде де нәтиже өзгеруі мүмкін.` : '';
    notify(`${result.message || 'Timetable generated.'} ${result.lesson_count ?? timetable.lessons.length} lessons saved.`);
    showPage('timetable');
  } finally {
    generating = false;
    buttons.forEach(button => { button.disabled = false; button.textContent = 'Generate timetable'; });
  }
}

function renderMusic() {
  const tracks = music.tracks;
  $('#track-list').replaceChildren();
  if (!tracks.length) $('#track-list').append(el('div', 'No tracks yet. Upload a music file to begin.', 'empty-state'));
  tracks.forEach(track => {
    const row = el('div', null, 'track-row');
    const name = el('div', null, 'track-name');
    name.append(el('span', track.original_name), el('small', `Track ${track.id}`));
    row.append(el('span', '♫', 'track-icon'), name);
    $('#track-list').append(row);
  });
  options($('#test-form [name=track_id]'), tracks, 'original_name');
  options($('#playlist-track'), tracks, 'original_name');
  const events = (music.event_types || Object.keys(EVENT_LABELS)).map(type => ({id: type, name: EVENT_LABELS[type] || type}));
  options($('#playlist-event'), events, 'name', null);
  loadPlaylistDraft();
}

function loadPlaylistDraft() {
  playlistDraft = [...(music.playlists[$('#playlist-event').value] || [])];
  $('#playlist-saved-note').textContent = '';
  renderPlaylist();
}

function renderPlaylist() {
  const container = $('#playlist-items');
  container.replaceChildren();
  if (!playlistDraft.length) container.append(el('div', 'This event has no playlist. Add a track below, or leave it silent.', 'empty-state'));
  playlistDraft.forEach((id, index) => {
    const track = music.tracks.find(item => item.id === id);
    const row = el('div', null, 'playlist-row');
    row.append(el('span', String(index + 1).padStart(2, '0'), 'playlist-index'), el('span', track?.original_name || `Track ${id}`, 'playlist-name'));
    const actions = [
      ['↑', 'Move track up', index === 0, () => { [playlistDraft[index - 1], playlistDraft[index]] = [playlistDraft[index], playlistDraft[index - 1]]; }],
      ['↓', 'Move track down', index === playlistDraft.length - 1, () => { [playlistDraft[index + 1], playlistDraft[index]] = [playlistDraft[index], playlistDraft[index + 1]]; }],
      ['×', 'Remove track from playlist', false, () => { playlistDraft.splice(index, 1); }]
    ];
    actions.forEach(([text, label, disabled, action]) => {
      const button = el('button', text, `small-button${text === '×' ? ' remove' : ''}`);
      button.type = 'button';
      button.setAttribute('aria-label', label);
      button.disabled = disabled;
      button.addEventListener('click', () => { action(); renderPlaylist(); $('#playlist-saved-note').textContent = 'Unsaved playlist changes.'; });
      row.append(button);
    });
    container.append(row);
  });
}

async function refreshPlayer() {
  if (playerPollPending) return;
  playerPollPending = true;
  try {
    const status = await api('/api/music/status');
    playerBusy = Boolean(status.busy);
    const heading = playerBusy ? (status.mode === 'dry-run' ? 'Simulated playback · no sound' : 'Playing on the server’s speakers') : (status.mode === 'idle' ? 'Ready to play' : 'Playback stopped');
    const lines = [heading];
    const trackName = status.original_name || status.track_name;
    if (trackName && playerBusy) lines.push(trackName);
    if (status.duration && playerBusy) lines.push(`${status.duration} seconds, including ${status.fade_out_seconds || 0} seconds of fade-out.`);
    lines.push(catalog?.settings.scheduler_enabled ? 'Automatic school bells are enabled.' : 'Automatic school bells are off.');
    if (status.error) lines.push(`Audio error: ${status.error}`);
    if (status.scheduler?.last_result?.status === 'error') lines.push(`Scheduler error: ${status.scheduler.last_result.message}`);
    $('#player-status').textContent = lines.join('\n');
    $('#player-status').classList.toggle('player-error', Boolean(status.error));
    if (status.error && status.error !== lastPlayerError) showError(new Error(`Speaker playback failed: ${status.error}`));
    lastPlayerError = status.error || null;
  } finally { playerPollPending = false; }
}

function renderBells() {
  const grids = bells.shift_periods || {'1': bells.periods};
  const rows = Object.entries(grids).flatMap(([shift, periods]) => periods.map(p => [
    `${shift}-ауысым · L${p.period}`, p.start, p.end,
    p.period === periods.length ? '—' : (p.period === catalog.settings.long_break_after ? '15 минут' : '5 минут')
  ]));
  table('#period-list', ['Сабақ', 'Басталуы', 'Аяқталуы', 'Үзіліс'], rows);
  $('#bell-priority').textContent = (bells.priority || []).map(type => EVENT_LABELS[type] || type).join(' → ');
  const day = Number($('#bell-day').value);
  table('#bell-list', ['Time', 'Event', 'Lesson'], bells.events.filter(event => event.weekday === day).map(event => [el('span', event.time, 'time-badge'), EVENT_LABELS[event.event_type] || event.event_type, `L${event.period}`]), 'No bells for this day. Generate a timetable first.');
}

function bindDataForm(selector, endpoint, payload, success, method = 'POST') {
  $(selector).addEventListener('submit', guard(async event => {
    event.preventDefault();
    if (!canInvalidate()) return;
    const form = event.currentTarget;
    const body = payload(form);
    await pending(form.querySelector('[type=submit]'), async () => {
      await api(typeof endpoint === 'function' ? endpoint(form) : endpoint, {method, body});
      form.reset();
      $('#lesson-editor').hidden = true;
      await refresh();
      notify(success);
    });
  }));
}

function bind() {
  $$('[data-page]').forEach(button => button.addEventListener('click', event => { event.preventDefault(); showPage(button.dataset.page); }));
  $$('[data-generate]').forEach(button => button.addEventListener('click', guard(generate)));
  $$('[data-stop]').forEach(button => button.addEventListener('click', guard(async () => {
    await api('/api/music/stop', {method: 'POST'});
    notify('Music stopped. Automatic scheduling remains at its current setting.');
    await refreshPlayer();
  })));
  $('#seed-button').addEventListener('click', guard(async event => {
    await pending(event.currentTarget, async () => { await api('/api/seed', {method: 'POST'}); await refresh(); notify('Demo school loaded. Generate the timetable to see all six classes.'); }, 'Loading demo…');
    $('#seed-button').disabled = catalog.grades.length > 0;
  }));
  bindDataForm('#subject-form', '/api/subjects', f => ({name: f.elements.name.value.trim(), is_hard: f.elements.is_hard.checked}), 'Subject added.');
  bindDataForm('#grade-form', '/api/grades', f => ({number: idValue(f, 'number'), parallel_count: idValue(f, 'parallel_count'), max_lessons_per_day: idValue(f, 'max_lessons_per_day'), language: f.elements.language.value, shift: idValue(f, 'shift')}), 'Grade and parallel classes created.');
  $('#class-form').addEventListener('submit', guard(async event => {
    event.preventDefault();
    if (!canInvalidate()) return;
    const form = event.currentTarget;
    const body = {name: form.elements.name.value.trim(), grade_id: idValue(form, 'grade_id'), language: form.elements.language.value, shift: idValue(form, 'shift')};
    await pending($('#class-save'), async () => {
      await api(editingClass ? `/api/classes/${editingClass}` : '/api/classes', {method: editingClass ? 'PUT' : 'POST', body});
      await refresh();
      notify('Сынып сақталды. Сабақ кестесін қайта құрыңыз.');
      $('#lesson-editor').hidden = true;
    });
    cancelClassEdit();
  }));
  $('#class-cancel').addEventListener('click', cancelClassEdit);
  bindDataForm('#room-form', '/api/rooms', f => ({name: f.elements.name.value.trim()}), 'Room added.');
  bindDataForm('#curriculum-form', '/api/curriculum', f => ({grade_id: idValue(f, 'grade_id'), subject_id: idValue(f, 'subject_id'), lessons_per_week: idValue(f, 'lessons_per_week')}), 'Weekly requirement saved.');
  bindDataForm('#assignment-form', '/api/assignments', f => ({class_id: idValue(f, 'class_id'), subject_id: idValue(f, 'subject_id'), teacher_id: idValue(f, 'teacher_id'), room_id: f.elements.room_id.value ? idValue(f, 'room_id') : null, lessons_per_week: f.elements.lessons_per_week.value ? idValue(f, 'lessons_per_week') : null}), 'Teaching assignment saved.');
  bindDataForm('#grade-limit-form', f => `/api/grades/${idValue(f, 'grade_id')}`, f => ({max_lessons_per_day: idValue(f, 'max_lessons_per_day')}), 'Grade daily limit updated.', 'PUT');
  $('#teacher-form').addEventListener('submit', guard(async event => {
    event.preventDefault();
    if (!canInvalidate()) return;
    const form = event.currentTarget;
    const body = {name: form.elements.name.value.trim(), subject_ids: [...form.elements.subject_ids.selectedOptions].map(option => Number(option.value))};
    await pending($('#teacher-save'), async () => {
      await api(editingTeacher ? `/api/teachers/${editingTeacher}` : '/api/teachers', {method: editingTeacher ? 'PUT' : 'POST', body});
      await refresh();
      notify(editingTeacher ? 'Teacher updated.' : 'Teacher registered.');
    });
    cancelTeacherEdit();
  }));
  $('#teacher-cancel').addEventListener('click', cancelTeacherEdit);
  $('#assignment-form [name=class_id]').addEventListener('change', updateAssignmentChoices);
  $('#assignment-form [name=subject_id]').addEventListener('change', updateTeacherChoices);
  $('#grade-limit-form [name=grade_id]').addEventListener('change', () => {
    const form = $('#grade-limit-form');
    const grade = catalog.grades.find(g => g.id === idValue(form, 'grade_id'));
    if (grade) form.elements.max_lessons_per_day.value = grade.max_lessons_per_day;
  });
  $('#view-type').addEventListener('change', () => { updateViewChoices(); renderTimetable(); $('#lesson-editor').hidden = true; });
  $('#view-id').addEventListener('change', () => { renderTimetable(); $('#lesson-editor').hidden = true; });
  $('#timetable-export').addEventListener('click', exportTimetable);
  $('#timetable-print').addEventListener('click', () => {
    if (selectedLessons().length) window.print();
  });
  $('#cancel-lesson-edit').addEventListener('click', () => { $('#lesson-editor').hidden = true; editingLesson = null; });
  $('#lesson-form').addEventListener('submit', guard(async event => {
    event.preventDefault();
    if (!editingLesson) return;
    const form = event.currentTarget;
    await pending(form.querySelector('[type=submit]'), async () => {
      await api(`/api/timetable/${editingLesson}`, {method: 'PATCH', body: {weekday: idValue(form, 'weekday'), period: idValue(form, 'period')}});
      $('#lesson-editor').hidden = true;
      editingLesson = null;
      await refresh();
      notify('Lesson moved. Timetable conflicts and weekly lesson counts were validated.');
    });
  }));
  $('#upload-form').addEventListener('submit', guard(async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const file = form.elements.file.files[0];
    if (file && file.size > 25 * 1024 * 1024) throw new Error('This file exceeds the 25 MB upload limit.');
    await pending(form.querySelector('[type=submit]'), async () => {
      await api('/api/music/upload', {method: 'POST', body: new FormData(form)});
      form.reset();
      music = await api('/api/music');
      renderMusic();
      renderOverview();
      notify('Track uploaded. Add it to an event playlist or test it now.');
    }, 'Uploading…');
  }));
  $('#test-form').addEventListener('submit', guard(async event => {
    event.preventDefault();
    const form = event.currentTarget;
    await pending(form.querySelector('[type=submit]'), async () => {
      await api('/api/music/test', {method: 'POST', body: {track_id: idValue(form, 'track_id')}});
      notify(catalog.settings.audio_enabled ? 'Test playback requested on the server’s speakers.' : 'Simulated playback started. Enable speaker output in settings for audible music.');
      await refreshPlayer();
    }, 'Starting…');
  }));
  $('#refresh-player').addEventListener('click', guard(refreshPlayer));
  $('#playlist-event').addEventListener('change', loadPlaylistDraft);
  $('#playlist-add').addEventListener('click', () => {
    const id = Number($('#playlist-track').value);
    if (!id) { showError(new Error('Choose a track to add.')); return; }
    playlistDraft.push(id);
    renderPlaylist();
    $('#playlist-saved-note').textContent = 'Unsaved playlist changes.';
  });
  $('#playlist-save').addEventListener('click', guard(async event => {
    const eventType = $('#playlist-event').value;
    if (!eventType) throw new Error('Choose a bell event first.');
    await pending(event.currentTarget, async () => {
      await api(`/api/music/playlists/${encodeURIComponent(eventType)}`, {method: 'PUT', body: {track_ids: playlistDraft}});
      music = await api('/api/music');
      $('#playlist-saved-note').textContent = 'Playlist saved. Rotation restarts from its first track.';
      notify(`${EVENT_LABELS[eventType] || eventType} playlist saved.`);
    });
  }));
  $('#bell-day').addEventListener('change', renderBells);
  $('#settings-form').addEventListener('submit', guard(async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const body = {};
    for (const name of ['start_time', 'second_shift_start_time', 'timezone']) body[name] = form.elements[name].value;
    for (const name of ['max_lessons_per_day', 'playback_duration', 'fade_out_seconds', 'solver_time_limit', 'long_break_after']) body[name] = idValue(form, name);
    for (const name of ['use_rooms', 'scheduler_enabled', 'audio_enabled', 'shifts_enabled']) body[name] = form.elements[name].checked;
    const structural = ['max_lessons_per_day', 'use_rooms'].some(key => body[key] !== catalog.settings[key]);
    if (structural && !canInvalidate()) return;
    await pending(form.querySelector('[type=submit]'), async () => {
      await api('/api/settings', {method: 'PUT', body});
      await refresh();
      notify('Settings saved. Bell times use the configured school timezone.');
    });
  }));
}

async function start() {
  bind();
  showPage(location.hash.slice(1) || 'overview');
  await refresh();
  await refreshPlayer();
  window.setInterval(() => {
    if (document.hidden || (!playerBusy && $('#page-music').hidden)) return;
    refreshPlayer().catch(error => { $('#player-status').textContent = `Unable to refresh player: ${error.message}`; });
  }, 1000);
}
start().catch(showError);
