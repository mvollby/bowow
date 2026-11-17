const sources = {
  canada: {name:'Government of Canada', mark:'CA', icon:'https://www.canada.ca/etc/designs/canada/wet-boew/assets/favicon.ico', url:'https://www.canada.ca/en/government/grants-funding.html'},
  us: {name:'Grants.gov', mark:'US', icon:'https://www.grants.gov/favicon.ico', url:'https://www.grants.gov/'},
  eu: {name:'European Commission', mark:'EU', icon:'https://commission.europa.eu/favicon.ico', url:'https://commission.europa.eu/funding-and-tenders_en'},
  foundation: {name:'Gates Foundation', mark:'GF', icon:'https://www.gatesfoundation.org/favicon.ico', url:'https://www.gatesfoundation.org/about/our-funding'}
};

const grants = [
  {name:'Rural Student Bursary Fund', summary:'Supports nonprofit programs that award need-based bursaries to rural students.', fit:'A direct fit for organizations funding students through need-based awards.', tags:['student aid','rural access'], score:8.8, source:sources.canada, region:'Canada', applicant:['Nonprofit'], purpose:'Student aid', funder:'Foundation', currency:'CAD', low:5000, high:30000, route:'Open call', match:'No match required', partners:'No partner required', deadline:'2027-02-26'},
  {name:'Youth Access & Learning Grant', summary:'Funds tutoring and access programs delivered by nonprofits and schools.', fit:'A potential fit for student support delivered through tutoring or mentoring. Budget for matching funds.', tags:['learning support'], score:7.9, source:sources.us, region:'United States', applicant:['Nonprofit','School'], purpose:'Program delivery', funder:'Government', currency:'USD', low:25000, high:100000, route:'Open call', match:'Match required', partners:'No partner required', deadline:'2027-04-16'},
  {name:'Cross-Border Education Partnership', summary:'Helps community organizations collaborate on education programs.', fit:'Most relevant for an education program that already has partners and matching funds.', tags:['learning support','partnerships'], score:6.8, source:sources.eu, region:'European Union', applicant:['Nonprofit','School'], purpose:'Program delivery', funder:'Government', currency:'EUR', low:40000, high:150000, route:'Open call', match:'Match required', partners:'Partner required', deadline:'2027-05-12'},
  {name:'Community Learning Capacity Fund', summary:'Provides operating support for nonprofits with established education programs.', fit:'Could support the work behind a student program if the organization is invited to apply.', tags:['operating costs'], score:6.2, source:sources.foundation, region:'International', applicant:['Nonprofit'], purpose:'Operating costs', funder:'Foundation', currency:'USD', low:15000, high:70000, route:'Invitation only', match:'No match required', partners:'No partner required', deadline:''},
  {name:'Student Research Travel Award', summary:'Helps students cover travel costs for a research placement.', fit:'Useful for an individual student seeking research travel money. The student would apply directly.', tags:['research'], score:5.6, source:sources.canada, region:'Canada', applicant:['Individual'], purpose:'Research', funder:'Foundation', currency:'CAD', low:500, high:2500, route:'Open call', match:'No match required', partners:'No partner required', deadline:'2027-03-19'}
];

const params = new URLSearchParams(location.search);
const filter = key => params.get(key) || '';
const topics = filter('topics').split(',');
const criteria = [
  ['region', 'Project location'],
  ['purpose', 'Funding purpose'],
  ['funder', 'Funder type'],
  ['currency', 'Currency'],
  ['route', 'Application route'],
  ['match', 'Matching funds'],
  ['partners', 'Partner requirement'],
  ['applicant', 'Applicant type', grant => grant.applicant.includes(filter('applicant'))],
  ['minimum', 'Minimum award', grant => grant.high >= Number(filter('minimum'))],
  ['maximum', 'Maximum award', grant => grant.low <= Number(filter('maximum'))],
  ['deadline', 'Deadline', grant => grant.deadline && grant.deadline <= filter('deadline')]
];
const activeCriteria = criteria.filter(([key]) => filter(key));
const matches = grants.map(grant => {
  const overlap = grant.tags.filter(tag => topics.includes(tag));
  const missed = activeCriteria.filter(([key, , check]) => check ? !check(grant) : grant[key] !== filter(key));
  const base = Math.min(99, Math.round(grant.score * 10) + overlap.length * 11);
  const rating = (missed.length ? Math.max(10, Math.min(59, base - missed.length * 17)) : base) / 10;
  return {...grant, overlap, rating, missed};
}).sort((a, b) => a.missed.length - b.missed.length || b.rating - a.rating || b.overlap.length - a.overlap.length);
const exactCount = matches.filter(grant => !grant.missed.length).length;

const editParams = new URLSearchParams(params);
editParams.delete('topics');
document.getElementById('edit-search').search = editParams.toString();
const leaders = matches[0];
const summary = document.getElementById('match-summary');
summary.textContent = exactCount
  ? leaders.name + ' leads the shortlist at ' + leaders.rating.toFixed(1) + '/10. ' + leaders.fit
  : 'No sample grants meet every filter. The closest options are shown below with lower scores and the filters they miss.';
if ((filter('minimum') || filter('maximum')) && !filter('currency')) {
  summary.textContent += ' Award amounts are compared in their listed currencies.';
}
document.getElementById('match-count').textContent = activeCriteria.length
  ? exactCount + ' exact · ' + (matches.length - exactCount) + ' partial'
  : matches.length + ' results';

const amount = new Intl.NumberFormat('en');
const date = new Intl.DateTimeFormat('en', {dateStyle:'medium', timeZone:'UTC'});
const list = document.getElementById('result-list');
for (const [index, grant] of matches.entries()) {
  const card = document.createElement('article');
  card.className = 'grant-card';
  card.innerHTML = `
    <div class="grant-body">
      <div class="provider"><span class="provider-mark" aria-hidden="true"></span><div><strong></strong><span class="provider-domain"></span></div></div>
      <h3></h3>
      <p class="grant-meta"></p>
      <p class="grant-description"></p>
      <p class="grant-fit"><strong>Why it fits</strong> <span></span></p>
      <p class="grant-misses" hidden><strong>Filter differences</strong> <span></span></p>
      <a class="grant-link" target="_blank" rel="noopener noreferrer">View funding source ↗</a>
    </div>
    <div class="grant-score"><div class="score-ring"><strong></strong><span>/ 10</span></div><span class="score-label"></span></div>`;
  const mark = card.querySelector('.provider-mark');
  mark.textContent = grant.source.mark;
  const icon = new Image();
  icon.alt = '';
  icon.referrerPolicy = 'no-referrer';
  icon.addEventListener('load', () => mark.replaceChildren(icon), {once:true});
  icon.src = grant.source.icon;
  card.querySelector('.provider strong').textContent = grant.source.name;
  card.querySelector('.provider-domain').textContent = new URL(grant.source.url).hostname;
  card.querySelector('h3').textContent = grant.name;
  card.querySelector('.grant-meta').textContent = [
    grant.currency + ' ' + amount.format(grant.low) + '–' + amount.format(grant.high),
    grant.region, grant.route, grant.deadline ? date.format(new Date(grant.deadline)) : 'Deadline unspecified'
  ].join(' · ');
  card.querySelector('.grant-description').textContent = grant.summary;
  card.querySelector('.grant-fit span').textContent = grant.fit + (grant.overlap.length ? ' Your program summary points to ' + grant.overlap.join(' and ') + '.' : '');
  if (grant.missed.length) {
    const differences = card.querySelector('.grant-misses');
    differences.hidden = false;
    differences.querySelector('span').textContent = grant.missed.map(([key, label]) => label + ' (' + filter(key) + ')').join(' · ');
  }
  card.querySelector('.grant-link').href = grant.source.url;

  const rating = grant.rating.toFixed(1);
  const ring = card.querySelector('.score-ring');
  ring.classList.add(grant.rating >= 8 ? 'strong-fit' : grant.rating >= 7 ? 'good-fit' : 'review-fit');
  ring.style.setProperty('--rating', grant.rating * 10 + '%');
  ring.querySelector('strong').textContent = rating;
  card.querySelector('.score-label').textContent = grant.missed.length ? 'Partial match' : index === 0 ? 'Best match' : grant.rating >= 8 ? 'Strong fit' : grant.rating >= 7 ? 'Good fit' : 'Review fit';
  list.append(card);
}
