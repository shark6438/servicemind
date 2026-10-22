<?php
require '/var/www/glpi/vendor/autoload.php';
$kernel = new \Glpi\Kernel\Kernel('production');$kernel->boot();
$name='[SECURITY-COMPLETION-20261022] Native webhook delivery fixture';
$ticket=new Ticket();
if(!$ticket->getFromDBByCrit(['name'=>$name,'entities_id'=>2])){
 $id=Session::callAsSystem(fn()=>$ticket->add(['name'=>$name,'content'=>'Synthetic assessment fixture. The VPN password is accepted; the second factor fails after replacing the handset. No production configuration change is authorized.','entities_id'=>2,'type'=>2,'status'=>1,'urgency'=>3,'impact'=>3]));
 if(!$id){throw new RuntimeException('Could not create the assessment fixture');}
 $ticket->getFromDB((int)$id);
}
$rows=(new QueuedWebhook())->find(['items_id'=>$ticket->getID(),'webhooks_id'=>2],['id DESC']);
if(!$rows){Webhook::raise('new',$ticket);$rows=(new QueuedWebhook())->find(['items_id'=>$ticket->getID(),'webhooks_id'=>2],['id DESC']);}
if(!$rows){throw new RuntimeException('Native GLPI event did not enqueue a delivery');}
$queue=reset($rows);
echo json_encode(['ticket_id'=>$ticket->getID(),'queue_id'=>$queue['id'],'webhook_id'=>2,'url'=>$queue['url'],'last_status_code'=>$queue['last_status_code']??null,'event'=>$queue['event'],'body_sha256'=>hash('sha256',$queue['body'])],JSON_PRETTY_PRINT),"\n";
