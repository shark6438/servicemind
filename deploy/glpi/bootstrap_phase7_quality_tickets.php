<?php
/**
 * Create the P7.6.6 quality-set tickets in the globex entity, idempotently, and print them.
 *
 * Eight read-only tickets, one per topical slice of the quality corpus. They exist because
 * a run is started against a ticket, and the quality set is 200 runs: without a small pool
 * of real tickets the driver has nothing to submit against.
 *
 * The content is deliberately thin. The quality cases are decided by what retrieval finds
 * for the *question*, which the driver passes as the run's goal; a ticket carrying its own
 * long narrative would put a second, uncontrolled body of text in front of the same
 * retrieval step and make a retrieval failure impossible to attribute. So each ticket
 * states the situation in one sentence and stops.
 *
 * No case writes a followup, so no ticket here owns a write -- unlike the acceptance
 * bootstrap, where a case per writing path is the point.
 *
 * Idempotent by ticket *name*: a ticket that already exists is left alone, so re-running
 * after a partial apply converges instead of duplicating. Prints one JSON object, which is
 * what the seeder reads back.
 *
 * Usage, inside the GLPI container:
 *   php /var/www/glpi/bootstrap_phase7_quality_tickets.php
 *
 * The bootstrap is the same one ``bootstrap_phase7_tickets.php`` uses: the vendor autoloader
 * plus a booted kernel. ``inc/includes.php`` alone does not put the ORM classes on the
 * autoloader in this image -- ``new Ticket()`` then dies with "Class not found" and the
 * script exits 255, which is how the first apply of this file failed. There is no session to
 * establish: the kernel boot is what makes the ORM usable from the CLI, and ``Session`` is
 * not autoloaded on this path at all.
 */

require '/var/www/glpi/vendor/autoload.php';

$kernel = new \Glpi\Kernel\Kernel('production');
$kernel->boot();

define('ENTITY_ID', 2);

//: ``Ticket::DEMAND_TYPE``, and named the same way ``bootstrap_phase7_tickets.php`` names it.
//: It was written as ``1`` first, which is ``INCIDENT_TYPE``: the constant was called
//: REQUEST and produced Incidents, so every quality run would have been filed against a
//: ticket whose type says an outage happened when the case is a question.
const TICKET_TYPE_REQUEST = 2;

//: One ticket per topical slice of the quality corpus. The ref is what the case list
//: declares; the name is what makes the apply idempotent.
const TICKETS = [
    'globex-quality-access' => '[P7.6-QUALITY-ACCESS] Remote access and identity question',
    'globex-quality-infra' => '[P7.6-QUALITY-INFRA] Database and storage question',
    'globex-quality-incident' => '[P7.6-QUALITY-INCIDENT] Incident handling question',
    'globex-quality-change' => '[P7.6-QUALITY-CHANGE] Change and release question',
    'globex-quality-security' => '[P7.6-QUALITY-SECURITY] Security policy question',
    'globex-quality-service' => '[P7.6-QUALITY-SERVICE] Service catalogue question',
    'globex-quality-restricted' => '[P7.6-QUALITY-RESTRICTED] Question answered only in a restricted runbook',
    'globex-quality-uncovered' => '[P7.6-QUALITY-UNCOVERED] Question with no matching runbook',
];

//: One sentence each, stating the situation and nothing that could answer the question.
const TICKET_CONTENT = <<<TEXT
A user has asked a question of the knowledge base. The question itself is the run goal.

No first-line action has been taken.
TEXT;

$resolved = [];
foreach (TICKETS as $ref => $name) {
    $ticket = new Ticket();
    if (!$ticket->getFromDBByCrit(['name' => $name, 'entities_id' => ENTITY_ID])) {
        $ticketId = $ticket->add([
            'name' => $name,
            'content' => TICKET_CONTENT,
            'entities_id' => ENTITY_ID,
            'type' => TICKET_TYPE_REQUEST,
            'status' => 1, // new
            'urgency' => 3,
            'impact' => 3,
        ]);
        if (!$ticketId) {
            fwrite(STDERR, "Unable to create ticket {$ref} in entity " . ENTITY_ID . ".\n");
            exit(1);
        }
        $ticket->getFromDB($ticketId);
    }
    if (!$ticket->getID()) {
        fwrite(STDERR, "Ticket {$ref} resolved to an unusable id.\n");
        exit(1);
    }
    $resolved[$ref] = (int) $ticket->getID();
}

// One line, like bootstrap_phase7_tickets.php: the seeder reads the *last* line of stdout
// back. It was written with JSON_PRETTY_PRINT first, which puts the closing brace on its own
// line -- the seeder then parsed "}" and died with "Expecting value: line 1 column 1".
echo json_encode([
    'entity_id' => ENTITY_ID,
    'tickets' => $resolved,
], JSON_UNESCAPED_UNICODE), "\n";
