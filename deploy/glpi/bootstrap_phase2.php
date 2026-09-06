<?php

declare(strict_types=1);

require '/var/www/glpi/vendor/autoload.php';

$kernel = new \Glpi\Kernel\Kernel('production');
$kernel->boot();

$result = ['Acme China' => 1, 'Globex China' => 2];
foreach ($result as $name => $entityId) {
    $entity = new Entity();
    if (!$entity->getFromDB($entityId)) {
        $createdId = $entity->add(['name' => $name, 'entities_id' => 0]);
        if ($createdId === false || (int) $createdId !== $entityId) {
            fwrite(STDERR, "Unable to create expected entity {$entityId} for {$name}.\n");
            exit(1);
        }
        $entity->getFromDB($entityId);
    }
    $entity->update(['id' => $entityId, 'name' => $name]);
}

$groups = [];
foreach ($result as $entityName => $entityId) {
    foreach (['Network Team', 'Service Desk'] as $groupName) {
        $group = new Group();
        if (!$group->getFromDBByCrit(['name' => $groupName, 'entities_id' => $entityId])) {
            $groupId = $group->add([
                'name' => $groupName,
                'entities_id' => $entityId,
                'is_recursive' => 0,
            ]);
            if ($groupId === false) {
                fwrite(STDERR, "Unable to create group {$groupName} for {$entityName}.\n");
                exit(1);
            }
            $group->getFromDB((int) $groupId);
        }
        $groups[$entityName][$groupName] = $group->getID();
    }
}

fwrite(STDOUT, json_encode(['entities' => $result, 'groups' => $groups], JSON_THROW_ON_ERROR) . "\n");
