<?php

declare(strict_types=1);

require '/var/www/glpi/vendor/autoload.php';

$kernel = new \Glpi\Kernel\Kernel('production');
$kernel->boot();

$clientId = getenv('SERVICEMIND_GLPI_CLIENT_ID');
$clientSecret = getenv('SERVICEMIND_GLPI_CLIENT_SECRET');
if ($clientId === false || $clientSecret === false) {
    fwrite(STDERR, "Missing ServiceMind OAuth bootstrap variables.\n");
    exit(1);
}

$client = new OAuthClient();
$exists = $client->getFromDBByCrit(['name' => 'ServiceMind Local Agent']);

if (!$exists) {
    $recordId = $client->add([
        'name' => 'ServiceMind Local Agent',
        'comment' => 'Local GLPI integration for the ServiceMind Agent MVP',
        'grants' => ['password'],
        'scopes' => ['api', 'graphql'],
        'redirect_uri' => [],
        'allowed_ips' => '',
        'is_active' => 1,
        'is_confidential' => 1,
    ]);
    if ($recordId === false) {
        fwrite(STDERR, "Unable to create the OAuth client.\n");
        exit(1);
    }
    $client->getFromDB((int) $recordId);
}

$updated = $client->update([
    'id' => $client->getID(),
    'identifier' => $clientId,
    'secret' => $clientSecret,
    'grants' => ['password'],
    'scopes' => ['api', 'graphql'],
    'redirect_uri' => [],
    'allowed_ips' => '',
    'is_active' => 1,
    'is_confidential' => 1,
]);

if (!$updated) {
    fwrite(STDERR, "Unable to configure the OAuth client.\n");
    exit(1);
}

fwrite(STDOUT, "ServiceMind OAuth client configured.\n");
