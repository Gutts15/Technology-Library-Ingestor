# E1: preparação segura de lotes (incremento isolado)

Este incremento implementa o construtor determinístico de lote em src/e1_lote_prepare.py. Não liga a entrada real, não agenda execução, não chama modelos e não escreve em 00_LIBRARY.

## Entrada e contrato

- Lê somente índices FILE_EVIDENCE existentes no Drive privado, envelopes ligados a hash e seus evidence-summary.json.
- Revalida o Stage 21 (ELIGIBLE/HELD) antes da seleção. HELD não entra no lote.
- Até 20 itens e até 200 KiB no lote. Evidência compactada por item até 3.000 bytes; até três pistas de equivalência no MASTER_INDEX, com títulos, IDs de registros e resumos curtos.
- Vincula cada item ao package_id, revision_key, hash da evidência e identificador opaco derivado dos três campos.
- Gera lote.json com schema_version, lote_id, created_at_utc, status PRONTO e itens. E2 deve calcular SHA-256 dos bytes efetivos do lote ao preparar decisoes.json.
- Evita selecionar novamente um identificador já presente em lote anterior; se houver lote PRONTO pendente, não cria lote novo. Falha fechada em inventário ou vínculo inválido.

## Uso somente em ambiente isolado

Leitura, sem escrita (padrão):

    python3 src/e1_lote_prepare.py --root-dir ./private-synthetic-fixture --date-brt 2026-10-27

Escrita isolada de teste:

    python3 src/e1_lote_prepare.py --root-dir ./private-synthetic-fixture --date-brt 2026-10-27 --apply

Existe opção --remote para infraestrutura de integração futura. NESTE PR, ela não é chamada por nenhum workflow. Não usar --remote --apply contra o Drive real antes de aprovar a integração E1/E2/E3.

## Limites de aceitação

- Este PR comprova apenas comportamento sintético/local ao passar nos testes.
- Sem comparação semântica de equivalência além da pista textual exata; jamais interpretar ausência de match como autorização de CREATE.
- Inventário imutável pode bloquear novos lotes até que E2/E3 implementem reconciliador transacional de estados. Isso é intencional: sem contorno manual ou sobrescrita.
- Não promove registros, não lê dados de projetos fora do escopo técnico e não exclui fontes.
- Ainda falta ligar E1 ao fluxo diário de ingestão real, E2 e E3, testar 7 dias consecutivos e validar consulta final.
