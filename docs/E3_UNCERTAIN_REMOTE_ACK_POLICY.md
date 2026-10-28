# Marco 2 — falhas de transporte com confirmação incerta

O publicador controlado E3 já tratava timeouts de subprocessos `rclone` como um resultado incerto e preservava o coordenador. O código, entretanto, ainda tratava resposta `moveto` não zero, cópia privada sem confirmação e leitura pós-escrita ausente como `False`. Uma resposta `False` podia cair no rollback genérico, mesmo quando a operação no Drive havia sido aplicada e sua confirmação se perdeu.

A alteração atual faz `RemoteUncertainExit(75)` nesses casos; portanto não executa rollback automático, nem libera o coordenador Git, nem repete a mutação. O processo encerra e deixa a transação sob posse ACTIVE para reconciliação posterior. A remoção canônica durante recuperação também preserva incerteza quando o comando de exclusão falha.

Essa política é conservadora: mesmo uma falha remota seguramente negativa é tratada como incerta quando não há evidência confiável. A recuperação normal deve reavaliar journal, hashes e dono do coordenador antes de decidir se houve escrita.

Os sete testes verificam cópia sem ACK, leitura incorreta, `moveto` com resposta não zero, leitura ausente após move, move confirmado e íntegro, falha de exclusão e bloqueio de caminho inseguro. Não utilizam credenciais, GitHub privado ou Drive real.

**Não é liberação de produção:** a prova do coordenador GitHub compartilhado e o fencing de gravações tardias permanecem pendentes. O `DROP_HERE` continua em modo fixture.
