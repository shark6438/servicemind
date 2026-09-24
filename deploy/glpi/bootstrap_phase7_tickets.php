<?php

declare(strict_types=1);

/**
 * Seed the Phase 7.6 acceptance tickets in the Globex entity.
 *
 * The acceptance cases are ticket-driven: a run needs a real ticket whose timeline carries
 * three specific facts -- the password step was accepted, the challenge step failed, and
 * the handset changed recently. Those facts are what the ACC-03 assertions look for, and
 * a ticket written into a database by hand would make the acceptance unreproducible on a
 * fresh volume. GLPI has no create-ticket method on the product's own client, so the
 * tickets are bootstrapped here, next to the entities and groups that bootstrap_phase2.php
 * creates.
 *
 * **One ticket per case that writes.** The six are structurally isomorphic -- same fields,
 * same three facts, same two seeded followups -- so a difference between any two of them
 * can only come from the run. What they are not is interchangeable, because a ticket
 * accumulates: a case that appends a followup leaves the ticket permanently different from
 * how the next run of the suite finds it.
 *
 * The reader set (``-a`` and ``-b``) is written to by nothing. The four writing cases own
 * ``-acc10b``, ``-acc11``, ``-acc22`` and ``-acc23`` outright, one each. The split is what
 * makes the suite re-runnable: no read-only case's evidence can be altered by a case that
 * ran before it, so the same batch produces the same observations in any order and on any
 * number of repetitions. Sharing was the earlier design and it showed up exactly here --
 * two writing cases on one ticket is two cases whose *inputs* differ between run one and
 * run two, and a read-only case sharing with a writer is a case whose verdict depends on
 * what ran before it.
 *
 * The first pair is still two tickets rather than one for the same reason it always was:
 * ACC-04b and ACC-09b run as a different subject, and a shared ticket would let one case's
 * write satisfy another's "exactly one new followup" assertion.
 *
 * Idempotent in the same way bootstrap_phase2.php is: a ticket is looked up by its exact
 * name inside the entity and left alone if it already exists, and the timeline is only
 * appended when the ticket has no followup yet. Re-running this never duplicates.
 *
 * It prints the resolved ticket ids as JSON on stdout so the host-side seeder can record
 * them next to the knowledge fixtures, which is where the driver resolves ``ticket_ref``.
 *
 *     docker compose exec -T glpi php /var/www/glpi/bootstrap_phase7_tickets.php
 */

require '/var/www/glpi/vendor/autoload.php';

$kernel = new \Glpi\Kernel\Kernel('production');
$kernel->boot();

const ENTITY_ID = 2;      // Globex China
const TICKET_TYPE_REQUEST = 2;

//: The three facts every acceptance ticket has to carry. Declared once rather than per
//: ticket: the whole point of the set is that no two of them differ, so a second copy of
//: this prose would be a second place for them to drift apart.
const TICKET_CONTENT = <<<'TEXT'
Reported by the user to the service desk.

The VPN client accepts the password and then fails the multi-factor authentication
challenge. The failure repeats on every attempt, from every network, and with the same
password. The user replaced their handset nine days ago. No lockout is recorded against
the account.

What is known so far: password authentication succeeded, multi-factor authentication
failed, and the user's phone was replaced recently.

No action has been taken yet.
TEXT;

//: The first-line followups that establish those facts, and that the ACC-02/ACC-03 style
//: evidence assertions read.
const TICKET_FOLLOWUPS = [
    'First line check: the account is not locked, and the password step is accepted on every attempt. The failure is at the second factor.',
    'The user confirms the handset was replaced nine days ago and that the old device was factory wiped before it was handed on.',
];

//: One ticket per case, keyed by the ref the case list declares. ``-a`` and ``-b`` are
//: read-only: no acceptance case appends to them. The four writing cases each own one.
const TICKETS = [
    'globex-vpn-mfa-a' => '[P7.6-ACCEPTANCE-A] VPN rejects the MFA challenge after a handset change',
    'globex-vpn-mfa-b' => '[P7.6-ACCEPTANCE-B] VPN rejects the MFA challenge after a handset change',
    'globex-vpn-mfa-acc10b' => '[P7.6-ACCEPTANCE-ACC-10B] VPN rejects the MFA challenge after a handset change',
    'globex-vpn-mfa-acc11' => '[P7.6-ACCEPTANCE-ACC-11] VPN rejects the MFA challenge after a handset change',
    'globex-vpn-mfa-acc22' => '[P7.6-ACCEPTANCE-ACC-22] VPN rejects the MFA challenge after a handset change',
    'globex-vpn-mfa-acc23' => '[P7.6-ACCEPTANCE-ACC-23] VPN rejects the MFA challenge after a handset change',
];

$tickets = [];
foreach (TICKETS as $ref => $name) {
    $tickets[] = [
        'ref' => $ref,
        'name' => $name,
        'content' => TICKET_CONTENT,
        'followups' => TICKET_FOLLOWUPS,
    ];
}

$resolved = [];
foreach ($tickets as $spec) {
    $ticket = new Ticket();
    if (!$ticket->getFromDBByCrit(['name' => $spec['name'], 'entities_id' => ENTITY_ID])) {
        $ticketId = $ticket->add([
            'name' => $spec['name'],
            'content' => $spec['content'],
            'entities_id' => ENTITY_ID,
            'type' => TICKET_TYPE_REQUEST,
            'status' => 1, // new
            'urgency' => 3,
            'impact' => 3,
        ]);
        if ($ticketId === false) {
            fwrite(STDERR, "Unable to create ticket {$spec['ref']} in entity " . ENTITY_ID . ".\n");
            exit(1);
        }
        $ticket->getFromDB((int) $ticketId);
    }

    $ticketId = (int) $ticket->getID();
    if ($ticketId <= 0) {
        fwrite(STDERR, "Ticket {$spec['ref']} resolved to an unusable id.\n");
        exit(1);
    }

    // Only append when the ticket has no timeline yet. Comparing followup content instead
    // would depend on GLPI's rich-text round trip being byte-stable, which it is not: the
    // check would re-add the timeline on every run while looking like an idempotency test.
    $existing = (new ITILFollowup())->find(['itemtype' => 'Ticket', 'items_id' => $ticketId]);
    if ($existing === []) {
        foreach ($spec['followups'] as $content) {
            $followup = new ITILFollowup();
            $followupId = $followup->add([
                'itemtype' => 'Ticket',
                'items_id' => $ticketId,
                'content' => $content,
                'is_private' => 0,
            ]);
            if ($followupId === false) {
                fwrite(STDERR, "Unable to append a followup to ticket {$spec['ref']}.\n");
                exit(1);
            }
        }
    }

    $resolved[$spec['ref']] = $ticketId;
}

fwrite(STDOUT, json_encode(['entity_id' => ENTITY_ID, 'tickets' => $resolved], JSON_THROW_ON_ERROR) . "\n");
