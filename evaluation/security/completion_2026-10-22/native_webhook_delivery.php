<?php
require '/var/www/glpi/vendor/autoload.php';
$kernel = new \Glpi\Kernel\Kernel('production');$kernel->boot();
$queueId=(int)getenv('SECURITY_QUEUE_ID');$ticketId=(int)getenv('SECURITY_TICKET_ID');
$queue=new QueuedWebhook();$ticket=new Ticket();
if(!$queue->getFromDB($queueId)||!$ticket->getFromDB($ticketId)||$queue->fields['items_id']!=$ticketId||$queue->fields['webhooks_id']!=2||!str_starts_with($ticket->fields['name'],'[SECURITY-COMPLETION-20261022]')){throw new RuntimeException('Not an owned assessment fixture');}
$originalBody=$queue->fields['body'];$url=$queue->fields['url'];
$webhook=new Webhook();$webhook->getFromDB(2);$secret=(new GLPIKey())->decrypt($webhook->fields['secret']);
$tenant='22222222-2222-4222-8222-222222222222';
function expectedRunId(string $tenant,string $signature):string{
 $namespace=hex2bin('6ba7b8119dad11d180b400c04fd430c8');
 $hex=substr(sha1($namespace.'servicemind:'.$tenant.':glpi-webhook:'.$signature),0,32);
 $hex[12]='5';$hex[16]=dechex((hexdec($hex[16])&3)|8);
 return substr($hex,0,8).'-'.substr($hex,8,4).'-'.substr($hex,12,4).'-'.substr($hex,16,4).'-'.substr($hex,20,12);
}
function frame(string $body,string $secret,int $timestamp):array{return ['X-GLPI-timestamp'=>(string)$timestamp,'X-GLPI-signature'=>Webhook::getSignature($body.(string)$timestamp,$secret)];}
function deliver(int $id,string $label,string $url,string $body,array $headers,string $tenant):array{
 $q=new QueuedWebhook();
 if(!$q->update(['id'=>$id,'url'=>$url,'body'=>$body,'headers'=>json_encode($headers),'last_status_code'=>0,'sent_try'=>100,'response_body'=>''])){throw new RuntimeException('Could not prepare owned delivery');}
 $sent=QueuedWebhook::sendById($id);$q->getFromDB($id);
 $response=json_decode((string)($q->fields['response_body']??''),true);
 return ['case'=>$label,'sender_return'=>$sent,'http_status'=>$q->fields['last_status_code'],'expected_run_id'=>expectedRunId($tenant,$headers['X-GLPI-signature']),'body_sha256'=>hash('sha256',$body),'response'=>$response];
}
$now=time();$fresh=frame($originalBody,$secret,$now);$rows=[];
$wrongUrl=str_replace($tenant,'11111111-1111-4111-8111-111111111111',$url);
$rows[]=deliver($queueId,'wrong-tenant-cra',$wrongUrl,$originalBody,$fresh,$tenant);
$changed=json_decode($originalBody,true,flags:JSON_THROW_ON_ERROR);$changed['item']['id']=$ticketId+100000;
$tampered=json_encode($changed,JSON_UNESCAPED_SLASHES|JSON_THROW_ON_ERROR);
$rows[]=deliver($queueId,'tampered-body',$url,$tampered,$fresh,$tenant);
$rows[]=deliver($queueId,'expired-signed-frame',$url,$originalBody,frame($originalBody,$secret,$now-301),$tenant);
$changed=json_decode($originalBody,true,flags:JSON_THROW_ON_ERROR);$changed['item']['entity']['id']=1;
$invalidScope=json_encode($changed,JSON_UNESCAPED_SLASHES|JSON_THROW_ON_ERROR);
$rows[]=deliver($queueId,'wrong-entity-valid-hmac',$url,$invalidScope,frame($invalidScope,$secret,$now),$tenant);
$rows[]=deliver($queueId,'native-valid-delivery',$url,$originalBody,$fresh,$tenant);
$rows[]=deliver($queueId,'same-signature-retry',$url,$originalBody,$fresh,$tenant);
$rows[]=deliver($queueId,'resigned-delivery',$url,$originalBody,frame($originalBody,$secret,max(time(),$now+1)),$tenant);
$queue->update(['id'=>$queueId,'sent_try'=>3]);
echo json_encode(['ticket_id'=>$ticketId,'queue_id'=>$queueId,'native_sender'=>'QueuedWebhook::sendById','cra_enabled'=>(bool)$webhook->fields['use_cra_challenge'],'frames'=>$rows],JSON_PRETTY_PRINT),"\n";
