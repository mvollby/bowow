/* Ranked results, backed by the real corpus in data/opportunities.json.
 *
 * The important change from the prototype: a filter can now come out three
 * ways, not two. It can match, it can miss, or the source may never have said.
 * The old version only had match and miss, so an unknown silently became a
 * match and the card claimed something nobody had checked. Eligibility is
 * where that is most expensive, so unknowns are shown, counted and ranked
 * below records we could actually verify.
 *
 * The score is a filter-match score, not a quality judgement. We have no
 * opinion on whether a grant is good, and inventing one would be dishonest,
 * so extraction confidence is shown next to the score rather than blended
 * into it.
 */

const DATA_URL = 'data/opportunities.json';

const params = new URLSearchParams(location.search);
const filter = key => params.get(key) || '';
const topics = filter('topics') ? filter('topics').split(',') : [];

// key, label, how to evaluate. A checker returns true, false, or null when
// the record does not carry the information.
const criteria = [
  ['region', 'Project location', g => g.regions.length ? g.regions.includes(filter('region')) : null],
  ['purpose', 'Funding purpose', g => g.purposes.length ? g.purposes.includes(filter('purpose')) : null],
  ['currency', 'Currency', g => g.currency ? g.currency === filter('currency') : null],
  ['route', 'Application route', g => g.route ? g.route === filter('route') : null],
  ['applicant', 'Applicant type', g => g.applicant.length ? g.applicant.includes(filter('applicant')) : null],
  ['minimum', 'Minimum award', g => g.high == null ? null : g.high >= Number(filter('minimum'))],
  ['maximum', 'Maximum award', g => g.low == null ? null : g.low <= Number(filter('maximum'))],
  ['deadline', 'Deadline', g => g.deadline ? g.deadline <= filter('deadline') : null],
  ['match', 'Matching funds', g => g.match_required === null ? null
    : (filter('match') === 'Match required') === g.match_required],
  ['partners', 'Partners', g => g.partner_required === null ? null
    : (filter('partners') === 'Partner required') === g.partner_required],
];

const activeCriteria = criteria.filter(([key]) => filter(key));

const amount = new Intl.NumberFormat('en');
const date = new Intl.DateTimeFormat('en', {dateStyle: 'medium', timeZone: 'UTC'});
const byId = id => document.getElementById(id);

const clamp = (low, high, n) => Math.max(low, Math.min(high, n));

/* Three bands that cannot overlap:
 *
 *   7.0 - 9.2   every active filter checked and matched
 *   4.0 - 6.9   nothing missed, but something could not be verified
 *   1.0 - 3.9   at least one filter missed
 *
 * Banding matters because the list sorts on verified-before-unverified. With
 * a single blended scale a verified 5.5 could sit above an unverified 5.7 and
 * the ranking would look broken even though it was right. Inside each band,
 * topic overlap lifts and each unresolved filter costs.
 */
function score(missed, unknown, overlap) {
  const lift = Math.min(22, overlap.length * 11);
  if (missed.length) return clamp(10, 39, 39 - missed.length * 9 + lift / 4) / 10;
  if (unknown.length) return clamp(40, 69, 69 - unknown.length * 8 + lift / 4) / 10;
  return clamp(70, 92, 70 + lift) / 10;
}

function evaluate(grant) {
  const overlap = (grant.tags || []).filter(tag => topics.includes(tag));
  const missed = [];
  const unknown = [];
  for (const [key, label, check] of activeCriteria) {
    const verdict = check(grant);
    if (verdict === null) unknown.push([key, label]);
    else if (!verdict) missed.push([key, label]);
  }
  return {...grant, overlap, missed, unknown, rating: score(missed, unknown, overlap)};
}

function render(payload) {
  const matches = payload.opportunities
    .map(evaluate)
    .sort((a, b) =>
      a.missed.length - b.missed.length ||
      a.unknown.length - b.unknown.length ||
      b.rating - a.rating ||
      b.overlap.length - a.overlap.length);

  const exact = matches.filter(g => !g.missed.length && !g.unknown.length).length;
  const partial = matches.filter(g => g.missed.length).length;
  const unsure = matches.length - exact - partial;

  // "Edit search" keeps the filters but drops derived params.
  const editParams = new URLSearchParams(params);
  editParams.delete('topics');
  byId('edit-search').search = editParams.toString();

  const summary = byId('match-summary');
  const leader = matches[0];
  if (!leader) {
    summary.textContent = 'The corpus is empty. Run the ingestion pipeline, then export again.';
  } else if (exact) {
    summary.textContent = `${leader.name} leads at ${leader.rating.toFixed(1)}/10, `
      + `matching every filter we could verify.`;
  } else if (partial === matches.length) {
    summary.textContent = 'No grant matches every filter. The closest are shown with the filters they miss.';
  } else {
    summary.textContent = `No grant matches every filter outright. `
      + `${unsure} could not be fully checked because the funder does not state every rule.`;
  }

  if ((filter('minimum') || filter('maximum')) && !filter('currency')) {
    summary.textContent += ' Award amounts are compared in their listed currencies.';
  }

  const counts = [];
  if (activeCriteria.length) {
    counts.push(`${exact} exact`);
    if (unsure) counts.push(`${unsure} unverified`);
    counts.push(`${partial} partial`);
  } else {
    counts.push(`${matches.length} results`);
  }
  byId('match-count').textContent = counts.join(' · ');

  const generated = new Date(payload.generated_at);
  byId('corpus-note').textContent =
    `${payload.count} live opportunities from ${payload.sources.join(', ')}, `
    + `exported ${date.format(generated)}. Scores rank how well a grant matches `
    + `your filters. They are not a judgement of the grant.`;

  const list = byId('result-list');
  list.replaceChildren();
  matches.forEach((grant, index) => list.append(card(grant, index)));
}

function card(grant, index) {
  const el = document.createElement('article');
  el.className = 'grant-card';
  el.innerHTML = `
    <div class="grant-body">
      <div class="provider"><span class="provider-mark" aria-hidden="true"></span><div><strong></strong><span class="provider-domain"></span></div></div>
      <h3></h3>
      <p class="grant-meta"></p>
      <p class="grant-description"></p>
      <p class="grant-fit"><strong>Why it fits</strong> <span></span></p>
      <p class="grant-misses" hidden><strong>Filter differences</strong> <span></span></p>
      <p class="grant-unknown" hidden><strong>Could not verify</strong> <span></span></p>
      <p class="grant-flags"></p>
      <a class="grant-link" target="_blank" rel="noopener noreferrer">View funding source ↗</a>
    </div>
    <div class="grant-score"><div class="score-ring"><strong></strong><span>/ 10</span></div><span class="score-label"></span></div>`;

  const mark = el.querySelector('.provider-mark');
  mark.textContent = grant.mark;
  if (grant.host) {
    const icon = new Image();
    icon.alt = '';
    icon.referrerPolicy = 'no-referrer';
    icon.addEventListener('load', () => mark.replaceChildren(icon), {once: true});
    icon.src = `https://${grant.host}/favicon.ico`;
  }
  el.querySelector('.provider strong').textContent = grant.funder;
  el.querySelector('.provider-domain').textContent = grant.host;
  el.querySelector('h3').textContent = grant.name;

  const range = grant.low != null && grant.high != null
    ? `${grant.currency} ${amount.format(grant.low)}–${amount.format(grant.high)}`
    : grant.high != null ? `${grant.currency} up to ${amount.format(grant.high)}`
    : 'Award amount unstated';
  el.querySelector('.grant-meta').textContent = [
    range,
    grant.regions.join(', ') || 'Region unstated',
    grant.route || 'Route unstated',
    grant.deadline ? date.format(new Date(grant.deadline)) : 'Deadline unspecified',
  ].join(' · ');

  el.querySelector('.grant-description').textContent =
    grant.summary || 'No description published.';

  // Built from what the record actually contains. No hand-written sales copy.
  const reasons = [];
  if (grant.applicant.length) reasons.push(`Open to ${grant.applicant.join(', ').toLowerCase()}`);
  if (grant.overlap.length) reasons.push(`your summary points to ${grant.overlap.join(' and ')}`);
  if (grant.match_required === false) reasons.push('no matching funds required');
  if (grant.partner_required === false) reasons.push('no partner required');
  el.querySelector('.grant-fit span').textContent =
    reasons.length ? reasons.join('; ') + '.' : 'Eligibility details are not published in a structured form.';

  if (grant.missed.length) {
    const node = el.querySelector('.grant-misses');
    node.hidden = false;
    node.querySelector('span').textContent =
      grant.missed.map(([key, label]) => `${label} (${filter(key)})`).join(' · ');
  }
  if (grant.unknown.length) {
    const node = el.querySelector('.grant-unknown');
    node.hidden = false;
    node.querySelector('span').textContent =
      grant.unknown.map(([, label]) => label).join(' · ')
      + ' — the funder does not state this. Confirm before applying.';
  }

  const flags = el.querySelector('.grant-flags');
  if (grant.confidence < 0.7) {
    const badge = document.createElement('span');
    badge.className = 'flag warn';
    badge.textContent = 'Eligibility read from prose, unverified';
    flags.append(badge);
  }
  const verified = document.createElement('span');
  verified.className = 'flag';
  verified.textContent = `Last checked ${date.format(new Date(grant.last_verified))}`;
  flags.append(verified);

  el.querySelector('.grant-link').href = grant.url;

  const ring = el.querySelector('.score-ring');
  // Colour follows the band, not the raw number, so the ring and the label
  // can never disagree about what kind of result this is.
  ring.classList.add(grant.missed.length ? 'review-fit'
    : grant.unknown.length ? 'unsure-fit'
    : grant.rating >= 8.1 ? 'strong-fit' : 'good-fit');
  ring.style.setProperty('--rating', grant.rating * 10 + '%');
  ring.querySelector('strong').textContent = grant.rating.toFixed(1);
  el.querySelector('.score-label').textContent =
    grant.missed.length ? 'Partial match'
    : grant.unknown.length ? 'Needs checking'
    : index === 0 ? 'Best match' : 'Verified match';
  return el;
}

fetch(DATA_URL)
  .then(response => {
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.json();
  })
  .then(render)
  .catch(error => {
    // A missing export is the normal first-run state, not a crash. Say what
    // to do about it instead of leaving the page blank.
    byId('match-summary').textContent = 'Could not load the grant corpus.';
    byId('corpus-note').textContent =
      `${DATA_URL} did not load (${error.message}). Generate it with: `
      + `cd pipeline, then python -m bowow_pipeline export`;
  });
