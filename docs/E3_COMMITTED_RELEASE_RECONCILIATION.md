# Marco 2 / E3 — reconciliação de commit já confirmado

Situação: componente inativo, sob validação CI. Não aciona publicações canônicas.

## Problema corrigido

O publicador anterior entrava no rollback genérico quando `release_committed` retornava erro mesmo depois de persistir o diário `COMMITTED`. Se o push do Git tivesse acontecido mas a confirmação não chegasse, havia risco de tentar desfazer conhecimento já confirmado.

## Contrato aplicado

- Uma transação cujo journal está `COMMITTED` **nunca** inicia rollback por erro de confirmação da liberação Git. O estado vira `COMMITTED_RELEASE_UNCERTAIN` e exige reconciliação.
- `candidate_committed_reconciliation.py` lê os bytes do journal/receipt, verifica SHA do recibo, identidade de transaction/batch/owner, todos os registros e índices atuais contra hashes de pós-transação e todos os snapshots presentes contra hashes anteriores.
- O recibo deve corresponder a cada item e ao MASTER_INDEX.
- O coordenador Git deve estar ACTIVE para o mesmo transaction_id/batch_id/owner/journal_path, ou FREE com prova de liberação da mesma transação e hash do recibo.
- A tentativa de liberação faz, no máximo, **um** push Git CAS e reconcilia com leitura. Se a resposta se perder, não repete push e não inicia rollback.
- A função `reconcile_live` é uma entrada de código privilegiada para execução posterior num worker autorizado. Não possui CLI nem workflow e não autoriza contornar a configuração de permissão do GitHub.

## Validação

A suíte `tests.test_candidate_committed_reconciliation` usa a transação CREATE efetiva da fixture E3 para verificar commit, snapshots, recibo e índice. Casos adversariais: arquivo externo alterado, snapshot corrompido, recibo adulterado, dono Git diferente, FREE pertencente a outro lote, perda de ACK com e sem push aplicado e hash ausente.

## Limites para fechar o marco 2

Ainda falta executar sob uma identidade GitHub autorizada a posse compartilhada e recuperação remota persistente (não o Git CAS local de teste). Permanecem desligados o DROP_HERE real e a publicação em 00_LIBRARY. A aceitação final V1 ainda não ocorreu.
