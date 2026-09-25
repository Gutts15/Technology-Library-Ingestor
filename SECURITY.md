# Security Policy

## Escopo

Segurança é uma camada de suporte do Technology Library Ingestor, não o objetivo principal do projeto.

A V1 usa controles pequenos, automáticos e de baixo custo de manutenção. Novos scanners, dependências ou mecanismos de segurança só entram quando reduzem um risco concreto sem transformar o pipeline em um sistema desnecessariamente complexo.

## Regra principal

Este repositório contém somente código, configuração genérica, documentação e testes sintéticos.

**Nunca commitar:**

- vídeos, screenshots ou documentos reais;
- transcrições ou OCR derivados de conteúdo privado;
- URLs privadas do Google Drive;
- IDs ou metadados que revelem conteúdo pessoal sem necessidade;
- cookies, tokens, OAuth credentials, service-account keys ou secrets;
- outputs brutos do Ingestor.

## Logs

Jobs de CI devem registrar apenas:

- identificador técnico não sensível;
- estágio atual;
- duração;
- quantidade de artefatos;
- status de sucesso/erro;
- mensagem de erro sanitizada.

Não imprimir transcrições, OCR, nomes de arquivos privados, conteúdo de frames ou valores de secrets.

## GitHub Actions

- começar com `permissions: contents: read`;
- não executar processamento privado em workflows disparados por Pull Requests externos;
- secrets nunca devem ser passados para código não confiável;
- não usar `actions/upload-artifact` para arquivos privados ou derivados sensíveis;
- concorrência inicial: 1 job de processamento por vez;
- actions de terceiros devem ser pinadas por commit SHA imutável;
- o workflow de segurança não recebe secrets nem acesso ao Google Drive.

## Security Gate V1

O gate principal é código nosso, sem dependências externas, e verifica apenas invariantes de alto valor:

- nenhum arquivo privado ou mídia real versionado;
- nenhum padrão de secret de alta confiança em arquivos rastreados;
- nenhuma construção Python de alto risco definida pela política;
- permissões explícitas e mínimas nos workflows;
- nenhuma action de terceiro sem SHA imutável;
- nenhum `upload-artifact` na V1;
- nenhum secret disponível ao próprio security gate.

O smoke test usa apenas dados sintéticos e inclui um valor-canário falso para confirmar que informações arbitrárias de ambiente não aparecem nos logs ou no manifesto.

## Ferramentas externas de segurança

Scanners externos são opcionais. Antes de adicionar um deles:

1. avaliar origem, licença, manutenção e dependências;
2. preferir versão/commit imutável e verificável;
3. executar sem secrets, sem Drive e sem arquivos privados;
4. conceder somente leitura;
5. manter apenas ferramentas que tragam cobertura relevante além do gate próprio.

Não existe meta de acumular scanners. Duas ferramentas independentes e bem isoladas são melhores do que uma coleção grande de cadeias de confiança desnecessárias.

## Dados temporários

O runner pode possuir arquivos privados apenas no filesystem temporário durante a execução. Ao final do job, o pipeline deve remover os artefatos locais sensíveis antes da finalização sempre que possível.

Resultados persistentes devem retornar somente ao armazenamento privado autorizado, nunca ao repositório.

## Publicação futura

Antes de mudar este repositório de privado para público:

1. revisar todo o histórico Git;
2. executar secret scanning independente;
3. revisar logs de Actions;
4. confirmar ausência de dados reais em commits e artifacts;
5. revisar licenças e atribuições de código de terceiros;
6. validar que nenhuma credencial histórica continua válida.
