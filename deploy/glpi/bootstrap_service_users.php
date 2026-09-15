<?php

declare(strict_types=1);

require '/var/www/glpi/vendor/autoload.php';

$kernel = new \Glpi\Kernel\Kernel('production');
$kernel->boot();
global $DB;

$accounts = [
    [
        'username' => getenv('SERVICEMIND_ACME_GLPI_USERNAME'),
        'password' => getenv('SERVICEMIND_ACME_GLPI_PASSWORD'),
        'entity_id' => 1,
    ],
    [
        'username' => getenv('SERVICEMIND_GLOBEX_GLPI_USERNAME'),
        'password' => getenv('SERVICEMIND_GLOBEX_GLPI_PASSWORD'),
        'entity_id' => 2,
    ],
];

foreach ($accounts as $account) {
    if (!$account['username'] || !$account['password']) {
        fwrite(STDERR, "Missing ServiceMind GLPI service-user variables.\n");
        exit(1);
    }
    $user = new User();
    if (!$user->getFromDBbyName($account['username'])) {
        $userId = $user->add([
            'name' => $account['username'],
            'password' => $account['password'],
            'password2' => $account['password'],
            'is_active' => 1,
        ]);
        if ($userId === false) {
            fwrite(STDERR, "Unable to create a ServiceMind GLPI user.\n");
            exit(1);
        }
        $user->getFromDB((int) $userId);
    }

    $binding = [
        'users_id' => $user->getID(),
        'profiles_id' => 6,
        'entities_id' => $account['entity_id'],
        'is_recursive' => 0,
    ];
    $assignments = iterator_to_array($DB->request([
        'SELECT' => ['id'],
        'FROM' => Profile_User::getTable(),
        'WHERE' => $binding,
        'ORDER' => ['id ASC'],
    ]));
    $profile = new Profile_User();
    if (count($assignments) === 0 && $profile->add($binding) === false) {
        fwrite(STDERR, "Unable to grant the scoped ServiceMind GLPI profile.\n");
        exit(1);
    }
    foreach (array_slice($assignments, 1) as $duplicate) {
        if (!$profile->delete(['id' => (int) $duplicate['id']], true)) {
            fwrite(STDERR, "Unable to remove a duplicate ServiceMind GLPI profile.\n");
            exit(1);
        }
    }
}

fwrite(STDOUT, "ServiceMind GLPI service users and entity profiles configured.\n");
