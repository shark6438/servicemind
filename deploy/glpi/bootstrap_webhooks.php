<?php

declare(strict_types=1);

require '/var/www/glpi/vendor/autoload.php';

$kernel = new \Glpi\Kernel\Kernel('production');
$kernel->boot();

$definitions = [
    [
        'name' => 'ServiceMind Acme Ticket Created',
        'tenant_id' => '11111111-1111-4111-8111-111111111111',
        'entity_id' => 1,
        'secret' => getenv('SERVICEMIND_ACME_WEBHOOK_SECRET'),
    ],
    [
        'name' => 'ServiceMind Globex Ticket Created',
        'tenant_id' => '22222222-2222-4222-8222-222222222222',
        'entity_id' => 2,
        'secret' => getenv('SERVICEMIND_GLOBEX_WEBHOOK_SECRET'),
    ],
];

foreach ($definitions as $definition) {
    if (!is_string($definition['secret']) || $definition['secret'] === '') {
        fwrite(STDERR, "Missing webhook secret for {$definition['name']}.\n");
        exit(1);
    }
    $payload = json_encode([
        'tenant_id' => $definition['tenant_id'],
        'event' => '{{ event }}',
        'item' => [
            'id' => '{{ item.id }}',
            'entity' => ['id' => '{{ item.entity.id }}'],
        ],
    ], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
    // Template values that must remain numeric cannot be JSON quoted.
    $payload = str_replace(
        ['"{{ item.id }}"', '"{{ item.entity.id }}"'],
        ['{{ item.id }}', '{{ item.entity.id }}'],
        $payload
    );

    $fields = [
        'name' => $definition['name'],
        'entities_id' => $definition['entity_id'],
        'is_recursive' => 0,
        'itemtype' => 'Ticket',
        'event' => 'new',
        'payload' => $payload,
        'use_default_payload' => 0,
        'custom_headers' => [],
        'url' => 'http://host.docker.internal:8080/v1/servicemind/webhooks/glpi',
        'secret' => $definition['secret'],
        'http_method' => 'post',
        'sent_try' => 3,
        'expiration' => 300,
        'is_active' => 1,
        'save_response_body' => 1,
        'log_in_item_history' => 1,
    ];

    $webhook = new Webhook();
    if ($webhook->getFromDBByCrit(['name' => $definition['name']])) {
        $fields['id'] = $webhook->getID();
        $ok = $webhook->update($fields);
    } else {
        $ok = $webhook->add($fields);
    }
    if ($ok === false) {
        fwrite(STDERR, "Unable to configure {$definition['name']}.\n");
        exit(1);
    }
    fwrite(STDOUT, "Configured {$definition['name']}.\n");
}
