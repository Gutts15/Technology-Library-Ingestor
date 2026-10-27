# E3 Marco 2 — decisão de retomada por prova

A camada de reconciliação do commit passa a expor `classify_restart`, uma função estritamente somente leitura que usa o journal persistente, o coordenador Git exato, os snapshots e os arquivos canônicos observados.

| Estado encontrado | Decisão sem mutação |
|---|---|
| PREPARED + dono ACTIVE antes do limite de escrita + todos os bytes anteriores preservados | SAFE_PREWRITE_ABORT |
| WRITING_RECORDS / REBUILDING_INDEXES / VERIFYING / RECOVERY_REQUIRED + dono ACTIVE armado + todos os hashes antes/depois válidos | RECOVER_UNCOMMITTED |
| COMMITTED com recibo e hashes íntegros + dono ACTIVE correto | RECONCILE_COMMITTED |
| COMMITTED já liberado no Git com mesmo recibo | ALREADY_RELEASED |
| RECOVERED com bytes anteriores e rollback verificado + dono ACTIVE | RELEASE_RECOVERED |
| Prova ausente, dono diferente, write boundary ambíguo, snapshot inválido, drift ou estado incompatível | BLOCKED |

Essa classificação não grava dados, não executa rollback e não libera o coordenador. É um roteador para os motores já existentes, sem criar uma segunda arquitetura. A execução remota automática completa continua dependendo da validação autorizada do coordenador GitHub compartilhado e do fencing de operações tardias.

Os testes usam a fixture E3 com commits de verdade em sistema de arquivos sintético e o contrato real dos journals, verificando os cinco estados principais e recusando inconsistências. Não requer GITHUB_TOKEN com permissão de escrita nem muda o workflow do DROP_HERE.
