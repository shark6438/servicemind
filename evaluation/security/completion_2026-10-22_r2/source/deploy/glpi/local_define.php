<?php

$target = getenv('SERVICEMIND_WEBHOOK_TARGET_URL');
$parts = is_string($target) ? parse_url($target) : false;
$valid = is_array($parts)
    && in_array($parts['scheme'] ?? '', ['http', 'https'], true)
    && ($parts['path'] ?? '') === '/v1/servicemind/webhooks/glpi'
    && !isset($parts['user'])
    && !isset($parts['pass'])
    && !isset($parts['query'])
    && !isset($parts['fragment']);
define('GLPI_SERVERSIDE_URL_ALLOWLIST', $valid ? ['~^' . preg_quote(rtrim($target, '/'), '~') . '(?:/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$~D'] : []);
