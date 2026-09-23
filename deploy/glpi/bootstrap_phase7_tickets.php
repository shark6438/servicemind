<?php

declare(strict_types=1);

/**
 * Seed the two Phase 7.6 acceptance tickets in the Globex entity.
 *
 * The acceptance cases are ticket-driven: a run needs a real ticket whose timeline carries
 * three specific facts -- the password step was accepted, the challenge step failed, and
 * the handset changed recently. Those facts are what the ACC-03 assertions look for, and
 * a ticket written into a database by hand would make the acceptance unreproducible on a
 * fresh volume. GLPI has no create-ticket method on the product's own client, so the
 * tickets are bootstrapped here, next to the entities and groups that bootstrap_phase2.php
 * creates.
 *
 * Two tickets, not one: ACC-04b and ACC-09b run as a different subject, and two cases
 * appending followups to the same ticket would let one case's write satisfy another's
 * "exactly one new followup" assertion. The two are structurally isomorphic -- same
 * fields, same three facts -- so a difference between them can only come from the run.
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

$tickets = [
    [
        'ref' => 'globex-vpn-mfa-a',
        'name' => '[P7.6-ACCEPTANCE-A] VPN rejects the MFA challenge after a handset change',
        'content' => <<<'TEXT'
        Reported by the user to the service desk.

        The VPN client accepts the password and then fails the multi-factor authentication
        challenge. The failure repeats on every attempt, from every network, and with the
        same password. The user replaced their handset nine days ago. No lockout is
        recorded against the account.

        What is known so far: password authentication succeeded, multi-factor
        authentication failed, and the user's phone was replaced recently.

        No action has been taken yet.
        TEXT,
        'followups' => [
            'First line check: the account is not locked, and the password step is accepted on every attempt. The failure is at the second factor.',
            'The user confirms the handset was replaced nine days ago and that the old device was factory wiped before it was handed on.',
        ],
    ],
    [
        'ref' => 'globex-vpn-mfa-b',
        'name' => '[P7.6-ACCEPTANCE-B] VPN rejects the MFA challenge after a handset change',
        'content' => <<<'TEXT'
        Reported by the user to the service desk.

        Authentication to the corporate VPN gets past the password and is then rejected at
        the multi-factor authentication step. It fails on every attempt, from every
        network, with the same password. The user's phone was replaced nine days ago.
        There is no lockout on the account.

        What is known so far: password authentication succeeded, multi-factor
        authentication failed, and the user's phone was replaced recently.

        No action has been taken yet.
        TEXT,
        'followups' => [
            'First line check: the account is not locked, and the password step is accepted on every attempt. The failure is at the second factor.',
            'The user confirms the handset was replaced nine days ago and that the old device was factory wiped before it was handed on.',
        ],
    ],
];

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
