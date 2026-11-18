# ViaLink

Aplicação local para consultar vídeos por URL e baixar MP4 ou MP3. O FastAPI serve a interface em HTML, CSS e JavaScript e a API na mesma porta. O sistema usa processos Python para o `yt-dlp`, o Deno para os desafios JavaScript do YouTube e o FFmpeg para produzir os arquivos. A instalação dispensa banco de dados, serviço pago, chave de API e etapa de build do frontend.

## Instalação no Windows

Use Python 3.13, os executáveis `ffmpeg` e `ffprobe` no `PATH` e, para YouTube, Deno 2.3 ou superior. Instale apenas o que estiver faltando. A distribuição Gyan inclui `libmp3lame`, usado para a saída MP3.

```powershell
winget install -e --id Python.Python.3.13 --source winget
winget install -e --id Gyan.FFmpeg --source winget
winget install -e --id DenoLand.Deno --source winget
```

Abra um novo PowerShell na pasta do projeto para carregar as mudanças do `PATH`.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn vialink.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Abra [http://127.0.0.1:8000](http://127.0.0.1:8000). Pressione **Ctrl+C** no PowerShell para encerrar. Se a porta estiver ocupada, troque `8000` no comando e no endereço. Se `py` estiver ausente, use `python -m venv .venv` depois de confirmar que `python --version` aponta para o Python instalado. `ffmpeg -version`, `ffprobe -version` e `deno --version` confirmam se os executáveis estão no `PATH`.

## Uso

1. Cole um link público de um vídeo e pressione **Enter** ou **Download**.
2. Escolha **Formato e qualidade** e clique em **Baixar arquivo**. As resoluções vêm das fontes encontradas pelo `yt-dlp`. O símbolo **≈** indica tamanho estimado. A opção MP3 usa 192 kb/s.
3. O site inicia a transferência ao chegar em **Arquivo pronto**. Use **Salvar arquivo** se o navegador não iniciar o download. Em caso de erro, o link e os formatos já consultados continuam disponíveis para uma nova tentativa.

Para testar, cole o [trecho público de Big Buck Bunny usado no exemplo de vídeo da W3Schools](https://www.w3schools.com/html/mov_bbb.mp4). Para reproduzir um erro de processamento, consulte [esta resposta de teste do HTTPBingo](https://httpbingo.org/response-headers?Content-Type=video%2Fmp4) e selecione MP4. O serviço declara `video/mp4`, mas o corpo é JSON. O aplicativo deve exibir a falha do FFmpeg junto ao botão do arquivo.

## Compatibilidade

- O ViaLink oferece faixas de mídia disponíveis por HTTP ou HTTPS direto. Para MP4, copia as faixas de vídeo e áudio compatíveis e junta as faixas separadas quando há áudio M4A. Para MP3, extrai e converte a melhor fonte de áudio disponível. Origens que só oferecem protocolos segmentados como HLS, DRM, transmissões ao vivo ou faixas grandes podem ficar sem opção de download.
- A prévia local usa uma fonte MP4 com até 8 MiB e compatibilidade com o navegador. Nos outros casos, o card mostra a miniatura quando disponível e o link para assistir na origem. Em links diretos pequenos, o `ffprobe` pode complementar duração e resolução e o FFmpeg pode gerar a miniatura.
- O `yt-dlp[default]` inclui uma versão compatível do pacote `yt-dlp-ejs`. O ViaLink habilita Deno quando o executável está no `PATH`, com os scripts da dependência instalada. YouTube pode exigir verificação, login ou PO Token para algumas origens, redes, clientes ou formatos. O aplicativo usa acesso público anônimo e a disponibilidade muda com a origem. Para acompanhar mudanças, atualize a versão declarada de `yt-dlp[default]` e repita as verificações antes de adotá-la.

## Processamento e limites

| Operação | Prazo | Concorrência | Retenção |
| --- | ---: | ---: | ---: |
| Consulta e prévia | 45 s | 3 | Até 32 resultados por 20 min |
| Preparação | 10 min | 2 | Até 6 arquivos por 15 min |

- Cada preparação aceita até 300 MB somando as faixas de entrada e até 300 MB no arquivo final. O supervisor limita o espaço temporário por tarefa, encerra a árvore de processos ao atingir o prazo e, no Windows, limita a memória comprometida da árvore a 768 MiB. O servidor limpa a pasta da tarefa em caso de erro e remove resultados expirados. Transferências abertas mantêm o arquivo até o fim. O encerramento normal remove a pasta temporária da sessão. Se o servidor principal for encerrado à força, a pasta `vialink-*` da sessão pode permanecer em `%TEMP%`.
- A API separa a análise, a preparação, a consulta de andamento e a entrega do arquivo. O backend reconsulta a origem e usa os identificadores das faixas aprovadas para aquela opção. Os estados de download vêm dos dados do `yt-dlp`; o processamento pelo FFmpeg exibe progresso indeterminado. Etapas rápidas podem terminar entre duas consultas da interface.
- O processo de mídia só usa HTTP(S) público. São verificados o endereço original, redirecionamentos, respostas DNS e o endereço IP usado na conexão. Faixas e prévias passam pelo mesmo transporte protegido. O FFmpeg recebe argumentos separados e abre somente arquivos locais com protocolo e demuxers definidos. A aplicação é destinada a um único processo Uvicorn em loopback, com estado em memória.
- Falhas mostram uma mensagem em português junto ao controle correspondente. O PowerShell do servidor recebe os detalhes técnicos, que podem incluir informações sobre a URL da origem.

## Verificação no Windows

- Interface conferida no navegador em 1280 px e em 390 px emulados. O teste cobriu o envio por Enter, o skeleton, a reprodução da prévia por teclado e os downloads MP4 e MP3 do trecho de demonstração. O `ffprobe` confirmou vídeo H.264 com áudio AAC no MP4 e áudio MP3 no outro arquivo.
- Consulta e download verificados com Deno 2.9.8. O `ffprobe` confirmou áudio AAC e vídeo H.264 em 360p e 480p. A tarefa de 480p passou pela etapa de junção das faixas. [Vídeo de Big Buck Bunny publicado pelo Blender no YouTube](https://www.youtube.com/watch?v=aqz-KE-bpKQ)
- Conferidos URL inválida, destino direto local e bloqueio de um redirecionamento privado por teste controlado do transporte. O conteúdo inválido do HTTPBingo gerou erro de processamento visível e permitiu nova tentativa com a mesma URL e opção.

## Fontes consultadas

- Dependências, escolha do runtime e limitações do YouTube. [Documentação do yt-dlp](https://github.com/yt-dlp/yt-dlp#dependencies), [configuração EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS), [instalação do Deno](https://docs.deno.com/runtime/getting_started/installation/) e [guia de PO Token](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- Instalação, mapeamento de faixas e restrição do protocolo de entrada. [Download do FFmpeg](https://ffmpeg.org/download.html), [documentação de linha de comando](https://ffmpeg.org/ffmpeg.html) e [protocolos do FFmpeg](https://ffmpeg.org/ffmpeg-protocols.html)
- Serviço da interface, supervisão de tarefas e liberação de recursos no FastAPI. [Arquivos estáticos](https://fastapi.tiangolo.com/tutorial/static-files/), [concorrência](https://fastapi.tiangolo.com/async/) e [ciclo de vida da aplicação](https://fastapi.tiangolo.com/advanced/events/)
- Skeleton, indicadores e erros junto à ação, segundo o Carbon Design System. [Carregamento](https://www.carbondesignsystem.com/building-blocks/core/components/loading/guidelines), [progresso](https://www.carbondesignsystem.com/building-blocks/core/components/progress-bar/guidelines) e [formulários](https://www.carbondesignsystem.com/building-blocks/core/components/form/guidelines)
- Regiões de status e movimento reduzido, segundo o W3C. [Mensagens de status](https://www.w3.org/WAI/WCAG21/Understanding/status-messages) e [animação a partir de interações](https://www.w3.org/WAI/WCAG22/Understanding/animation-from-interactions)
