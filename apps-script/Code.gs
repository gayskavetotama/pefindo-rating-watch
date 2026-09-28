/**
 * PEFINDO Rating Watch - email relay.
 *
 * Runs in your own Google account (script.google.com) and sends the alert emails
 * from your Gmail. The GitHub checker POSTs each alert here with a secret key.
 *
 * Edit the three settings below, then Deploy > New deployment > Web app
 * (Execute as: Me, Who has access: Anyone). Recipients never leave this script.
 */

// Shared secret: must equal the MAIL_KEY GitHub secret. Replace with the key Claude gave you.
const KEY = 'PASTE-MAIL-KEY-HERE';

// Get the full email, including which alerts hit the BCA Life watchlist.
const TEAM = [
  'your.name@gmail.com',
  // 'colleague1@company.co.id',
];

// Get the same email with all holdings information removed.
const OTHERS = [
  // 'someone@example.com',
];

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply('ERROR bad request');
  }
  if (body.key !== KEY) return reply('ERROR bad key');

  let sent = 0;
  sent += deliver(TEAM, body.team);
  sent += deliver(OTHERS, body.others);
  return reply('OK sent to ' + sent + ' recipients, quota left ' + MailApp.getRemainingDailyQuota());
}

// Recipients go in BCC so colleagues don't see each other's addresses.
function deliver(list, msg) {
  const to = list.filter(String);
  if (!to.length || !msg) return 0;
  MailApp.sendEmail({
    to: Session.getEffectiveUser().getEmail(),
    bcc: to.join(','),
    subject: msg.subject,
    htmlBody: msg.html,
    name: 'PEFINDO Rating Watch',
  });
  return to.length;
}

function reply(text) {
  return ContentService.createTextOutput(text);
}

// Run this once from the editor (select testSend > Run) to approve Gmail access and see a test email.
function testSend() {
  const html = '<p>Test from PEFINDO Rating Watch relay. If you can read this, the relay works.</p>';
  deliver(TEAM, { subject: '[PEFINDO TEST] relay works (team list)', html: html });
  deliver(OTHERS, { subject: '[PEFINDO TEST] relay works (others list)', html: html });
}
