const byId = id => document.getElementById(id);
const uploadGroups = new Map();
const fileStatus = byId('file-status');

for (const area of document.querySelectorAll('[data-upload]')) {
  const group = area.dataset.upload;
  const input = area.querySelector('input');
  const list = byId(group + '-files');
  const files = new Map();
  uploadGroups.set(group, files);

  function renderFiles() {
    list.replaceChildren();
    for (const [key, file] of files) {
      const item = document.createElement('li');
      const name = document.createElement('span');
      name.textContent = file.name;
      name.title = file.name;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.setAttribute('aria-label', 'Remove ' + file.name);
      remove.addEventListener('click', () => {
        files.delete(key);
        renderFiles();
        area.querySelector('.pick').focus();
      });
      item.append(name, remove);
      list.append(item);
    }
    const count = [...uploadGroups.values()].reduce((total, selected) => total + selected.size, 0);
    fileStatus.textContent = count ? count + (count === 1 ? ' file selected' : ' files selected') + ' · Files stay in this browser session. This prototype does not read their contents.' : '';
  }

  function addFiles(selected) {
    for (const file of selected) {
      files.set(JSON.stringify([file.name, file.size, file.lastModified]), file);
    }
    renderFiles();
  }

  area.querySelector('.pick').addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    addFiles(input.files);
    input.value = '';
  });
  area.addEventListener('dragover', event => {
    event.preventDefault();
    area.classList.add('is-over');
  });
  area.addEventListener('dragleave', event => {
    if (!area.contains(event.relatedTarget)) area.classList.remove('is-over');
  });
  area.addEventListener('drop', event => {
    event.preventDefault();
    area.classList.remove('is-over');
    addFiles(event.dataTransfer.files);
  });
}

document.addEventListener('dragover', event => {
  if (event.dataTransfer.types.includes('Files')) event.preventDefault();
});
document.addEventListener('drop', event => {
  if (event.dataTransfer.types.includes('Files')) event.preventDefault();
});

const filterFields = [...byId('filters').querySelectorAll('input,select')];
const minimum = byId('minimum');
const maximum = byId('maximum');

function validateRange() {
  maximum.setCustomValidity(minimum.value !== '' && maximum.value !== '' && minimum.valueAsNumber > maximum.valueAsNumber ? 'Maximum award must be at least the minimum.' : '');
}
minimum.addEventListener('input', validateRange);
maximum.addEventListener('input', validateRange);
byId('clear').addEventListener('click', () => {
  for (const field of filterFields) field.value = '';
  validateRange();
});

const current = new URLSearchParams(location.search);
const filters = byId('filters');
const searchForm = byId('search');
for (const field of filterFields) {
  if (current.has(field.id)) field.value = current.get(field.id);
}

searchForm.addEventListener('submit', event => {
  event.preventDefault();
  validateRange();
  if (filterFields.some(field => !field.validity.valid)) filters.open = true;
  if (!searchForm.reportValidity()) return;
  const params = new URLSearchParams();
  for (const field of filterFields) {
    if (field.value) params.set(field.id, field.value);
  }
  // Concept names and triggers mirror bowow_pipeline/topics.py, which is the
  // canonical vocabulary. Note the phrases: an earlier version matched the
  // bare word "operating", so a summary saying a charity was "operating from
  // one site" was tagged as seeking operating costs.
  const summary = byId('program').value.toLowerCase();
  const topics = [
    ['student aid', /bursar|scholarship|tuition|financial aid|student award/],
    ['learning support', /tutor|mentor|literacy|numeracy|after school|homework|essential skills|adult learning/],
    ['rural access', /\brural\b|remote communit|northern communit|underserved/],
    ['indigenous', /indigenous|first nation|inuit|metis|métis|aboriginal/],
    ['newcomers', /newcomer|immigrant|refugee|settlement service|language training/],
    ['youth', /\byouth\b|young people|adolescent/],
    ['employment skills', /employment|workforce|skills training|job readiness|apprenticeship/],
    ['operating costs', /operating costs?|core funding|overhead|operational funding|organizational capacity/],
    ['research', /research|fieldwork/],
    ['partnerships', /partnership|consortium|collaborative|coalition/],
    ['infrastructure', /infrastructure|renovation|capital project|equipment/],
    ['arts culture', /\barts\b|culture|heritage|museum|festival/],
    ['health', /health|wellness|wellbeing/],
    ['food security', /food security|nutrition|food bank/],
    ['environment', /environment|climate|conservation|biodiversity/]
  ].filter(([, pattern]) => pattern.test(summary)).map(([topic]) => topic);
  if (topics.length) params.set('topics', topics.join(','));
  const target = new URL(searchForm.action);
  target.search = params.toString();
  location.assign(target);
});

for (const trigger of document.querySelectorAll('[data-dialog]')) {
  trigger.addEventListener('click', () => byId(trigger.dataset.dialog).showModal());
}
