<?php

// Local development allowlist: permit only the ServiceMind webhook endpoint on
// Docker Desktop's host gateway. Keep private-network destinations blocked.
define('GLPI_SERVERSIDE_URL_ALLOWLIST', [
    '~^http://host\.docker\.internal:8080/v1/servicemind/webhooks/glpi$~',
]);
