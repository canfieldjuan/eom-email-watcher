(async () => {
  const select = selector => document.querySelector(selector);
  const click = selector => {
    const element = select(selector);
    if (!element || element.disabled) throw Error('Unavailable control: ' + selector);
    element.click();
  };
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const pause = delay => new Promise(resolve => setTimeout(resolve, delay));
  const wait = async (ready, message) => {
    for (let until = performance.now() + 25000; performance.now() < until;) {
      if (ready()) return;
      await pause(100);
    }
    throw Error(message + '. Inbox status: ' + select('#inbox-status')?.textContent);
  };
  const rows = () => [...document.querySelectorAll('#inbox-list [data-inbox-thread-origin]')];
  const cards = () => [...document.querySelectorAll('#inbox-thread-view .thread-card')];
  const panel = () => select('#inbox-filter-form').matches(':popover-open');
  const snapshot = async () => {
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const panes = ['#inbox-sender-results', '#inbox-results', '#inbox-reading-pane'].map(selector => {
      const element = select(selector);
      const { x, y, width, height } = element.getBoundingClientRect();
      return [x, y, width, height, element.scrollLeft, element.scrollTop];
    });
    return { viewport: [innerWidth, innerHeight], range: document.documentElement.scrollHeight,
      scroll: [scrollX, scrollY], panes };
  };

  await wait(() => select('#inbox-tab') && !select('#inbox-tab').disabled, 'Inbox did not become available');
  const admission = await window.__TAURI_INTERNALS__.invoke('config_admission_status');
  check(admission.state === 'admitted', 'Configuration admission is ' + admission.state);
  check(location.hostname === 'tauri.localhost' || location.protocol === 'tauri:', 'Expected the packaged Tauri URL');
  click('#inbox-tab');
  await wait(() => rows().length === 2 && select('#inbox-status').dataset.kind === 'success', 'Expected both conversations');

  const before = await snapshot();
  click('#inbox-filters-toggle');
  check(panel(), 'Filters did not open');
  const open = await snapshot();
  click('#inbox-filters-close');
  check(!panel(), 'Filters did not close');
  const after = await snapshot();
  check(JSON.stringify(before) === JSON.stringify(open) && JSON.stringify(before) === JSON.stringify(after),
    'Filters shifted document or pane geometry: ' + JSON.stringify({ before, open, after }));

  click('#inbox-filters-toggle');
  select('#inbox-keyword').value = 'qualification';
  click('#inbox-filter-form button[type="submit"]');
  await wait(() => rows().length === 1 && rows()[0].innerText.includes('SENT qualification reply'), 'Filter did not keep the synthetic chain');
  rows()[0].click();
  await wait(() => cards().length === 2 && cards().every(card => card.querySelector('.inbox-message-extras')), 'Expected actions for both retained emails');
  const observed = cards().map(card => ({
    subject: card.querySelector('h4').textContent,
    text: card.querySelector('.inbox-message-extras').innerText,
    deleteAnchors: Array.from(card.querySelectorAll('[data-inbox-action="delete"]'), action =>
      JSON.parse(action.dataset.threadFocus.slice('inbox-action:'.length))),
  }));
  const expected = [
    ['SENT qualification reply', 'sent-proof', 'reply-sent.pdf', 'quote-inbound.pdf'],
    ['INBOX qualification quote', 'inbound-proof', 'quote-inbound.pdf', 'reply-sent.pdf'],
  ];
  expected.forEach(([subject, id, attachment, siblingAttachment], index) => {
    const item = observed[index];
    check(item.subject === subject && item.text.includes(attachment) && !item.text.includes(siblingAttachment)
      && JSON.stringify(item.deleteAnchors) === JSON.stringify([[id, 'delete']]),
      'Wrong per-email action ownership: ' + JSON.stringify(item));
  });
  return { pass: true, url: location.href, admission: admission.state, before, open, after, observed };
})()
