# E3 — prova de commit em segundo processo usando Drive real isolado

A prova de CREATE + UPDATE na mesma transação agora exige um teste adicional antes de limpar a pasta sintética.

Após a gravação do journal COMMITTED, recibo e índices, o processo original inicia um novo interpretador Python. O novo interpretador usa exclusivamente as leituras da pasta privada sintética no Drive para reconstruir e verificar as provas de commit:
- SHA do journal/receipt e identidade do lote/transação;
- hashes reais de pós-escrita de todos os registros e índices;
- snapshots originais e respectivos hashes;
- coordenador Git local (descartável) em FREE com recibo correspondente à mesma transação.

O segundo processo não emite mutações canônicas nem tenta repetir a liberação do Git; se o coordenador aparecer ACTIVE, pertencente a outra transação ou inacessível, ele bloqueia o teste. Somente após confirmação ocorre o purge restrito ao prefixo aleatório da fixture.

A workflow existente `e3-integrated-commit-acceptance.yml` é reutilizada para essa prova. Nenhum GITHUB_TOKEN com contents:write, novo agendamento ou permissão extraordinária foi adicionado.

Limite permanente: a prova usa o **Git CAS local**, não o ref compartilhado GitHub, cuja configuração continua bloqueada pela ferramenta. Não conclui o marco 2 por si só nem autoriza ativar DROP_HERE ou escrever na biblioteca real.
