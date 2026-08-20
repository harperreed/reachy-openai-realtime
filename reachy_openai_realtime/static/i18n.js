(() => {
  "use strict";

  const CODES = ["en", "ja", "zh", "ko", "es", "fr", "de", "it", "pt"];
  const LOCALES = {
    en: "en-US", ja: "ja-JP", zh: "zh-CN", ko: "ko-KR", es: "es-ES",
    fr: "fr-FR", de: "de-DE", it: "it-IT", pt: "pt-PT",
  };
  const rows = {
    lead: ["Conversation & connection status", "会話・接続状況", "对话与连接状态", "대화 및 연결 상태", "Conversación y conexión", "Conversation et connexion", "Gesprächs- und Verbindungsstatus", "Conversazione e connessione", "Conversa e ligação"],
    loading: ["Checking…", "確認中…", "正在检查…", "확인 중…", "Comprobando…", "Vérification…", "Wird geprüft…", "Verifica…", "A verificar…"],
    checking: ["Checking", "確認中", "正在检查", "확인 중", "Comprobando", "Vérification", "Wird geprüft", "Verifica", "A verificar"],
    loading_robot: ["Loading robot status", "ロボットの状態を読み込んでいます", "正在加载机器人状态", "로봇 상태를 불러오는 중", "Cargando el estado del robot", "Chargement de l’état du robot", "Roboterstatus wird geladen", "Caricamento dello stato del robot", "A carregar o estado do robô"],
    api_disconnected: ["○ API disconnected", "○ API未接続", "○ API未连接", "○ API 연결 안 됨", "○ API desconectada", "○ API déconnectée", "○ API nicht verbunden", "○ API disconnessa", "○ API desligada"],
    api_connected: ["● Realtime API connected", "● Realtime API接続済み", "● Realtime API已连接", "● Realtime API 연결됨", "● API Realtime conectada", "● API Realtime connectée", "● Realtime API verbunden", "● API Realtime connessa", "● API Realtime ligada"],
    key_checking: ["Checking API key", "キー確認中", "正在检查API密钥", "API 키 확인 중", "Comprobando la clave API", "Vérification de la clé API", "API-Schlüssel wird geprüft", "Verifica della chiave API", "A verificar a chave API"],
    key_configured: ["✓ API key configured", "✓ APIキー設定済み", "✓ API密钥已设置", "✓ API 키 설정됨", "✓ Clave API configurada", "✓ Clé API configurée", "✓ API-Schlüssel eingerichtet", "✓ Chiave API configurata", "✓ Chave API configurada"],
    key_unconfigured: ["○ API key not configured", "○ APIキー未設定", "○ API密钥未设置", "○ API 키 미설정", "○ Clave API sin configurar", "○ Clé API non configurée", "○ API-Schlüssel nicht eingerichtet", "○ Chiave API non configurata", "○ Chave API não configurada"],
    mic_aria: ["Microphone input level", "マイク入力レベル", "麦克风输入电平", "마이크 입력 레벨", "Nivel de entrada del micrófono", "Niveau d’entrée du microphone", "Mikrofon-Eingangspegel", "Livello di ingresso del microfono", "Nível de entrada do microfone"],
    mic_input: ["Microphone input", "マイク入力", "麦克风输入", "마이크 입력", "Entrada del micrófono", "Entrée microphone", "Mikrofoneingang", "Ingresso microfono", "Entrada do microfone"],
    usage_title: ["API usage", "API使用量", "API用量", "API 사용량", "Uso de la API", "Utilisation de l’API", "API-Nutzung", "Utilizzo API", "Utilização da API"],
    estimate: ["ESTIMATE", "推定", "估算", "추정", "ESTIMACIÓN", "ESTIMATION", "SCHÄTZUNG", "STIMA", "ESTIMATIVA"],
    cumulative_tokens: ["Cumulative tokens", "累計トークン", "累计令牌", "누적 토큰", "Tokens acumulados", "Jetons cumulés", "Kumulierte Tokens", "Token cumulativi", "Tokens acumulados"],
    estimated_cost: ["Estimated cost", "推定料金", "预估费用", "예상 비용", "Coste estimado", "Coût estimé", "Geschätzte Kosten", "Costo stimato", "Custo estimado"],
    input_tokens: ["Input tokens", "入力トークン", "输入令牌", "입력 토큰", "Tokens de entrada", "Jetons d’entrée", "Eingabe-Tokens", "Token di input", "Tokens de entrada"],
    output_tokens: ["Output tokens", "出力トークン", "输出令牌", "출력 토큰", "Tokens de salida", "Jetons de sortie", "Ausgabe-Tokens", "Token di output", "Tokens de saída"],
    cached_input: ["Cached input", "キャッシュ入力", "缓存输入", "캐시된 입력", "Entrada en caché", "Entrée en cache", "Zwischengespeicherte Eingabe", "Input nella cache", "Entrada em cache"],
    responses: ["Responses", "応答回数", "响应次数", "응답 수", "Respuestas", "Réponses", "Antworten", "Risposte", "Respostas"],
    usage_modalities: ["Audio in/out {audioIn}/{audioOut} · Text in/out {textIn}/{textOut} · Image in {image}", "音声 入/出 {audioIn}/{audioOut}・テキスト 入/出 {textIn}/{textOut}・画像 入力 {image}", "音频 输入/输出 {audioIn}/{audioOut} · 文本 输入/输出 {textIn}/{textOut} · 图像输入 {image}", "오디오 입력/출력 {audioIn}/{audioOut} · 텍스트 입력/출력 {textIn}/{textOut} · 이미지 입력 {image}", "Audio ent./sal. {audioIn}/{audioOut} · Texto ent./sal. {textIn}/{textOut} · Imagen entrada {image}", "Audio entrée/sortie {audioIn}/{audioOut} · Texte entrée/sortie {textIn}/{textOut} · Image entrée {image}", "Audio Ein/Aus {audioIn}/{audioOut} · Text Ein/Aus {textIn}/{textOut} · Bild-Eingabe {image}", "Audio in/out {audioIn}/{audioOut} · Testo in/out {textIn}/{textOut} · Immagine in {image}", "Áudio entrada/saída {audioIn}/{audioOut} · Texto entrada/saída {textIn}/{textOut} · Imagem entrada {image}"],
    usage_tracking_since: ["Tracking since {time}", "集計開始: {time}", "统计起始时间：{time}", "집계 시작: {time}", "Seguimiento desde {time}", "Suivi depuis le {time}", "Erfasst seit {time}", "Monitoraggio dal {time}", "Registo desde {time}"],
    usage_notice: ["Measured from response.done events stored on this robot. The USD estimate uses the model rates configured in this app as of {date} and may differ from the final bill.", "このロボットが保存したresponse.doneの実測値です。ドル額はこのアプリに{date}時点で設定したモデル単価による推定で、最終請求と異なる場合があります。", "根据此机器人保存的response.done事件实测。美元金额按此应用截至{date}配置的模型价格估算，可能与最终账单不同。", "이 로봇에 저장된 response.done 이벤트의 실측값입니다. 달러 금액은 이 앱에 {date} 기준으로 설정된 모델 요금에 따른 추정치이며 최종 청구액과 다를 수 있습니다.", "Medido a partir de eventos response.done guardados en este robot. El importe en USD usa las tarifas configuradas en esta aplicación a fecha de {date} y puede diferir de la factura final.", "Mesuré à partir des événements response.done enregistrés sur ce robot. Le montant en USD utilise les tarifs configurés dans cette application au {date} et peut différer de la facture finale.", "Gemessen anhand der auf diesem Roboter gespeicherten response.done-Ereignisse. Der USD-Betrag verwendet die in dieser App zum Stand {date} hinterlegten Modellpreise und kann von der Rechnung abweichen.", "Misurato dagli eventi response.done salvati su questo robot. L’importo in USD usa le tariffe configurate nell’app alla data {date} e può differire dalla fattura finale.", "Medido através dos eventos response.done guardados neste robô. O valor em USD usa os preços configurados nesta aplicação em {date} e pode diferir da fatura final."],
    conversation_language: ["Conversation language", "会話言語", "对话语言", "대화 언어", "Idioma de conversación", "Langue de conversation", "Gesprächssprache", "Lingua di conversazione", "Idioma da conversa"],
    target_language: ["Language", "対象言語", "语言", "언어", "Idioma", "Langue", "Sprache", "Lingua", "Idioma"],
    language_notice: ["English is the default. Changes apply from the next response and persist after restart.", "初期値は英語です。変更は次の応答から反映され、再起動後も保持されます。", "默认语言为英语。更改将从下一次回复开始生效，并在重启后保留。", "기본 언어는 영어입니다. 변경 사항은 다음 응답부터 적용되며 재시작 후에도 유지됩니다.", "El idioma predeterminado es inglés. Los cambios se aplican desde la siguiente respuesta y se conservan tras reiniciar.", "L’anglais est la langue par défaut. Les changements s’appliquent dès la prochaine réponse et sont conservés après redémarrage.", "Englisch ist die Standardsprache. Änderungen gelten ab der nächsten Antwort und bleiben nach einem Neustart erhalten.", "La lingua predefinita è l’inglese. Le modifiche si applicano dalla risposta successiva e restano dopo il riavvio.", "O idioma predefinido é o inglês. As alterações aplicam-se a partir da resposta seguinte e mantêm-se após reiniciar."],
    ai_camera: ["AI camera", "AIカメラ", "AI摄像头", "AI 카메라", "Cámara con IA", "Caméra IA", "KI-Kamera", "Fotocamera IA", "Câmara com IA"],
    unavailable: ["Unavailable", "利用不可", "不可用", "사용 불가", "No disponible", "Indisponible", "Nicht verfügbar", "Non disponibile", "Indisponível"],
    camera_state_on: ["ON · sends when speech starts", "ON・発話時送信", "开启 · 发言时发送", "켜짐 · 발화 시 전송", "ON · envía al empezar a hablar", "ON · envoi au début de la parole", "AN · Versand bei Sprechbeginn", "ON · invio all’inizio del parlato", "ON · envia ao começar a falar"],
    off: ["OFF", "OFF", "关闭", "꺼짐", "OFF", "OFF", "AUS", "OFF", "OFF"],
    camera_on: ["Turn camera on", "カメラをON", "开启摄像头", "카메라 켜기", "Activar cámara", "Activer la caméra", "Kamera einschalten", "Attiva fotocamera", "Ligar câmara"],
    camera_off: ["Turn camera off", "カメラをOFF", "关闭摄像头", "카메라 끄기", "Desactivar cámara", "Désactiver la caméra", "Kamera ausschalten", "Disattiva fotocamera", "Desligar câmara"],
    camera_preview_alt: ["Reachy Mini camera preview", "Reachy Miniのカメラプレビュー", "Reachy Mini摄像头预览", "Reachy Mini 카메라 미리보기", "Vista previa de la cámara de Reachy Mini", "Aperçu de la caméra de Reachy Mini", "Vorschau der Reachy-Mini-Kamera", "Anteprima della fotocamera di Reachy Mini", "Pré-visualização da câmara do Reachy Mini"],
    camera_send_status: ["Sent to OpenAI: {count} images{suffix}", "OpenAI送信: {count}枚{suffix}", "已发送至OpenAI：{count}张{suffix}", "OpenAI 전송: {count}장{suffix}", "Enviadas a OpenAI: {count} imágenes{suffix}", "Envoyées à OpenAI : {count} images{suffix}", "An OpenAI gesendet: {count} Bilder{suffix}", "Inviate a OpenAI: {count} immagini{suffix}", "Enviadas para a OpenAI: {count} imagens{suffix}"],
    camera_send_last: [" · last {time}", "・最終 {time}", " · 最近 {time}", " · 최근 {time}", " · última {time}", " · dernière {time}", " · zuletzt {time}", " · ultima {time}", " · última {time}"],
    camera_send_next: [" · sends when the next speech starts", "・次の発話開始時に送信", " · 下次发言开始时发送", " · 다음 발화 시작 시 전송", " · se enviará al empezar a hablar", " · envoi au prochain début de parole", " · Versand beim nächsten Sprechbeginn", " · invio al prossimo inizio del parlato", " · envia no início da próxima fala"],
    camera_notice: ["The camera is off by default. When enabled, one still image is sent to the OpenAI Realtime API when human speech starts.", "初期状態はOFFです。ONにすると、人の発話開始時に静止画を1枚OpenAI Realtime APIへ送信します。", "摄像头默认关闭。开启后，检测到人开始说话时会向OpenAI Realtime API发送一张静止图像。", "카메라는 기본적으로 꺼져 있습니다. 켜면 사람이 말하기 시작할 때 정지 이미지 한 장을 OpenAI Realtime API로 전송합니다.", "La cámara está desactivada de forma predeterminada. Al activarla, se envía una imagen fija a la API Realtime de OpenAI cuando una persona empieza a hablar.", "La caméra est désactivée par défaut. Lorsqu’elle est activée, une image fixe est envoyée à l’API Realtime d’OpenAI au début de la parole.", "Die Kamera ist standardmäßig ausgeschaltet. Wenn sie aktiviert ist, wird bei Sprechbeginn ein Standbild an die OpenAI Realtime API gesendet.", "La fotocamera è disattivata per impostazione predefinita. Se attivata, invia un’immagine fissa all’API Realtime di OpenAI quando una persona inizia a parlare.", "A câmara está desligada por predefinição. Quando ativada, envia uma imagem fixa para a API Realtime da OpenAI quando uma pessoa começa a falar."],
    error: ["Error", "エラー", "错误", "오류", "Error", "Erreur", "Fehler", "Errore", "Erro"],
    recent_conversation: ["Recent conversation", "直近の会話", "最近对话", "최근 대화", "Conversación reciente", "Conversation récente", "Letztes Gespräch", "Conversazione recente", "Conversa recente"],
    you: ["You", "あなた", "你", "나", "Tú", "Vous", "Du", "Tu", "Tu"],
    not_spoken: ["You haven't spoken yet", "まだ話しかけていません", "你还没有说话", "아직 말하지 않았습니다", "Aún no has hablado", "Vous n’avez pas encore parlé", "Du hast noch nicht gesprochen", "Non hai ancora parlato", "Ainda não falaste"],
    audio_sent_no_transcript: ["Audio input sent (no transcription)", "音声入力を送信しました（文字起こしなし）", "音频输入已发送（无转录）", "오디오 입력 전송됨(텍스트 변환 없음)", "Entrada de audio enviada (sin transcripción)", "Entrée audio envoyée (sans transcription)", "Audioeingabe gesendet (ohne Transkription)", "Ingresso audio inviato (senza trascrizione)", "Entrada de áudio enviada (sem transcrição)"],
    waiting_response: ["Waiting for a response", "応答待ちです", "正在等待回复", "응답 대기 중", "Esperando una respuesta", "En attente d’une réponse", "Warten auf eine Antwort", "In attesa di una risposta", "A aguardar uma resposta"],
    motion: ["Motion", "モーション", "动作", "모션", "Movimiento", "Mouvement", "Bewegung", "Movimento", "Movimento"],
    last_updated: ["Last updated", "最終更新", "最后更新", "마지막 업데이트", "Última actualización", "Dernière mise à jour", "Zuletzt aktualisiert", "Ultimo aggiornamento", "Última atualização"],
    model: ["Model", "モデル", "模型", "모델", "Modelo", "Modèle", "Modell", "Modello", "Modelo"],
    voice: ["Voice", "音声", "语音", "음성", "Voz", "Voix", "Stimme", "Voce", "Voz"],
    app: ["App", "アプリ", "应用", "앱", "Aplicación", "Application", "App", "App", "Aplicação"],
    activity_log: ["Activity log", "動作ログ", "活动日志", "동작 로그", "Registro de actividad", "Journal d’activité", "Aktivitätsprotokoll", "Registro attività", "Registo de atividade"],
    loading_log: ["Loading log…", "ログを読み込んでいます…", "正在加载日志…", "로그 불러오는 중…", "Cargando registro…", "Chargement du journal…", "Protokoll wird geladen…", "Caricamento registro…", "A carregar o registo…"],
    no_logs: ["No log entries yet", "ログはまだありません", "暂无日志", "아직 로그가 없습니다", "Aún no hay registros", "Aucune entrée pour le moment", "Noch keine Einträge", "Nessuna voce nel registro", "Ainda não há registos"],
    diagnostics: ["Diagnostics", "診断", "诊断", "진단", "Diagnóstico", "Diagnostic", "Diagnose", "Diagnostica", "Diagnóstico"],
    diagnostics_notice: ["View all Realtime events, two microphone channels, DoA speech detection, VAD thresholds, and send/commit counts.", "全Realtimeイベント、マイク2ch、DoA人声判定、VAD閾値、送信・commit回数を確認できます。", "可查看所有Realtime事件、双麦克风通道、DoA语音检测、VAD阈值以及发送/提交次数。", "모든 Realtime 이벤트, 마이크 2채널, DoA 음성 감지, VAD 임계값, 전송/커밋 횟수를 확인합니다.", "Consulta todos los eventos Realtime, los dos canales de micrófono, la detección de voz DoA, los umbrales VAD y los recuentos de envío/confirmación.", "Consultez tous les événements Realtime, les deux canaux micro, la détection vocale DoA, les seuils VAD et les compteurs d’envoi/validation.", "Alle Realtime-Ereignisse, zwei Mikrofonkanäle, DoA-Spracherkennung, VAD-Schwellen sowie Sende-/Commit-Zähler anzeigen.", "Visualizza tutti gli eventi Realtime, i due canali microfono, il rilevamento vocale DoA, le soglie VAD e i conteggi di invio/commit.", "Veja todos os eventos Realtime, os dois canais de microfone, a deteção de voz DoA, os limiares VAD e as contagens de envio/commit."],
    open_diagnostics: ["Open diagnostics JSON", "診断JSONを開く", "打开诊断JSON", "진단 JSON 열기", "Abrir JSON de diagnóstico", "Ouvrir le JSON de diagnostic", "Diagnose-JSON öffnen", "Apri JSON diagnostico", "Abrir JSON de diagnóstico"],
    copy_diagnostics: ["Copy diagnostics", "診断ログをコピー", "复制诊断信息", "진단 로그 복사", "Copiar diagnóstico", "Copier le diagnostic", "Diagnose kopieren", "Copia diagnostica", "Copiar diagnóstico"],
    api_settings: ["OpenAI API settings", "OpenAI API設定", "OpenAI API设置", "OpenAI API 설정", "Configuración de la API de OpenAI", "Paramètres de l’API OpenAI", "OpenAI-API-Einstellungen", "Impostazioni API OpenAI", "Definições da API OpenAI"],
    show: ["Show", "表示", "显示", "표시", "Mostrar", "Afficher", "Anzeigen", "Mostra", "Mostrar"],
    hide: ["Hide", "隠す", "隐藏", "숨기기", "Ocultar", "Masquer", "Ausblenden", "Nascondi", "Ocultar"],
    save_to_robot: ["Save to robot", "ロボットに保存", "保存到机器人", "로봇에 저장", "Guardar en el robot", "Enregistrer sur le robot", "Auf dem Roboter speichern", "Salva nel robot", "Guardar no robô"],
    remove_key: ["Remove saved key", "保存済みキーを削除", "删除已保存的密钥", "저장된 키 삭제", "Eliminar clave guardada", "Supprimer la clé enregistrée", "Gespeicherten Schlüssel entfernen", "Rimuovi chiave salvata", "Remover chave guardada"],
    key_notice: ["The key is stored in this Reachy Mini's persistent app settings. Its saved value cannot be read back through the UI or API.", "キーはこのReachy Mini内の永続的なアプリ設定領域に保存されます。画面やAPIから保存値を読み戻すことはできません。", "密钥保存在此Reachy Mini的持久应用设置中，无法通过界面或API读回已保存的值。", "키는 이 Reachy Mini의 영구 앱 설정에 저장됩니다. UI나 API에서 저장된 값을 다시 읽을 수 없습니다.", "La clave se guarda en la configuración persistente de esta Reachy Mini. Su valor no puede recuperarse desde la interfaz ni la API.", "La clé est enregistrée dans les paramètres persistants de ce Reachy Mini. Sa valeur ne peut pas être relue via l’interface ou l’API.", "Der Schlüssel wird dauerhaft in den App-Einstellungen dieses Reachy Mini gespeichert. Der gespeicherte Wert kann weder über die Oberfläche noch über die API ausgelesen werden.", "La chiave viene salvata nelle impostazioni permanenti di questo Reachy Mini. Il valore salvato non può essere riletto dall’interfaccia o dall’API.", "A chave é guardada nas definições persistentes deste Reachy Mini. O valor guardado não pode ser lido novamente pela interface ou pela API."],

    phase_starting: ["Starting", "起動中", "正在启动", "시작 중", "Iniciando", "Démarrage", "Wird gestartet", "Avvio", "A iniciar"],
    phase_waiting_key: ["Waiting for API key", "APIキー待ち", "等待API密钥", "API 키 대기", "Esperando la clave API", "En attente de la clé API", "Warten auf API-Schlüssel", "In attesa della chiave API", "A aguardar a chave API"],
    phase_starting_audio: ["Preparing audio", "音声準備中", "正在准备音频", "오디오 준비 중", "Preparando audio", "Préparation de l’audio", "Audio wird vorbereitet", "Preparazione audio", "A preparar o áudio"],
    phase_tuning_audio: ["Tuning microphone", "マイク調整中", "正在调节麦克风", "마이크 조정 중", "Ajustando el micrófono", "Réglage du microphone", "Mikrofon wird abgestimmt", "Regolazione microfono", "A ajustar o microfone"],
    phase_connecting: ["Connecting to API", "API接続中", "正在连接API", "API 연결 중", "Conectando a la API", "Connexion à l’API", "API-Verbindung wird hergestellt", "Connessione all’API", "A ligar à API"],
    phase_reconnecting: ["Reconnecting", "再接続中", "正在重新连接", "재연결 중", "Reconectando", "Reconnexion", "Verbindung wird wiederhergestellt", "Riconnessione", "A restabelecer a ligação"],
    phase_listening: ["Ready to talk", "話しかけてOK", "可以开始说话", "말해 주세요", "Listo para hablar", "Prêt à parler", "Bereit zum Sprechen", "Pronto a parlare", "Pronto para falar"],
    phase_user_speaking: ["Listening", "聞き取り中", "正在聆听", "듣는 중", "Escuchando", "Écoute", "Hört zu", "In ascolto", "A ouvir"],
    phase_thinking: ["Understanding", "理解中", "正在理解", "이해 중", "Comprendiendo", "Compréhension", "Verarbeitet", "Comprensione", "A compreender"],
    phase_responding: ["Generating response", "応答中", "正在生成回复", "응답 생성 중", "Generando respuesta", "Génération de la réponse", "Antwort wird erstellt", "Generazione risposta", "A gerar a resposta"],
    phase_assistant_speaking: ["Speaking", "発話中", "正在说话", "말하는 중", "Hablando", "Parle", "Spricht", "Sta parlando", "A falar"],
    phase_disconnected: ["Disconnected", "接続切れ", "已断开连接", "연결 끊김", "Desconectado", "Déconnecté", "Getrennt", "Disconnesso", "Desligado"],
    phase_error: ["Error", "エラー", "错误", "오류", "Error", "Erreur", "Fehler", "Errore", "Erro"],
    phase_stopped: ["Stopped", "停止中", "已停止", "중지됨", "Detenido", "Arrêté", "Gestoppt", "Arrestato", "Parado"],

    detail_starting: ["Starting the app", "アプリを起動しています", "正在启动应用", "앱을 시작하는 중", "Iniciando la aplicación", "Démarrage de l’application", "App wird gestartet", "Avvio dell’app", "A iniciar a aplicação"],
    detail_waiting_key: ["Set your OpenAI API key", "OpenAI APIキーを設定してください", "请设置OpenAI API密钥", "OpenAI API 키를 설정하세요", "Configura tu clave API de OpenAI", "Configurez votre clé API OpenAI", "OpenAI-API-Schlüssel einrichten", "Configura la chiave API OpenAI", "Configure a chave API da OpenAI"],
    detail_starting_audio: ["Preparing the microphone and speaker", "マイクとスピーカーを準備しています", "正在准备麦克风和扬声器", "마이크와 스피커를 준비하는 중", "Preparando el micrófono y el altavoz", "Préparation du microphone et du haut-parleur", "Mikrofon und Lautsprecher werden vorbereitet", "Preparazione di microfono e altoparlante", "A preparar o microfone e o altifalante"],
    detail_tuning_audio: ["Tuning the Wireless microphone", "Wirelessマイクを調整しています", "正在调节Wireless麦克风", "Wireless 마이크를 조정하는 중", "Ajustando el micrófono Wireless", "Réglage du microphone Wireless", "Wireless-Mikrofon wird abgestimmt", "Regolazione del microfono Wireless", "A ajustar o microfone Wireless"],
    detail_connecting: ["Connecting to the Realtime API", "Realtime APIへ接続しています", "正在连接Realtime API", "Realtime API에 연결하는 중", "Conectando a la API Realtime", "Connexion à l’API Realtime", "Verbindung zur Realtime API wird hergestellt", "Connessione all’API Realtime", "A ligar à API Realtime"],
    detail_reconnecting: ["Restarting the Realtime session", "Realtimeセッションを再起動しています", "正在重启Realtime会话", "Realtime 세션을 다시 시작하는 중", "Reiniciando la sesión Realtime", "Redémarrage de la session Realtime", "Realtime-Sitzung wird neu gestartet", "Riavvio della sessione Realtime", "A reiniciar a sessão Realtime"],
    detail_listening: ["Speak in {language}", "{language}で話しかけてください", "请用{language}说话", "{language}로 말해 주세요", "Habla en {language}", "Parlez en {language}", "Sprich auf {language}", "Parla in {language}", "Fale em {language}"],
    detail_listening_connected: ["Connected. Speak in {language}", "接続済み。{language}で話しかけてください", "已连接。请用{language}说话", "연결됨. {language}로 말해 주세요", "Conectado. Habla en {language}", "Connecté. Parlez en {language}", "Verbunden. Sprich auf {language}", "Connesso. Parla in {language}", "Ligado. Fale em {language}"],
    detail_user_speaking: ["Listening to you", "音声を聞いています", "正在听你说话", "음성을 듣고 있습니다", "Te estoy escuchando", "Je vous écoute", "Ich höre zu", "Ti sto ascoltando", "Estou a ouvir"],
    detail_turn_silence: ["Speech confirmed after 800 ms of silence", "発話を確定しました（無音800ms）", "检测到800毫秒静音，发言已确认", "800ms 무음 후 발화 확정", "Intervención confirmada tras 800 ms de silencio", "Parole confirmée après 800 ms de silence", "Äußerung nach 800 ms Stille bestätigt", "Intervento confermato dopo 800 ms di silenzio", "Fala confirmada após 800 ms de silêncio"],
    detail_turn_maximum: ["Speech confirmed at the 20-second limit", "発話を確定しました（発話上限20秒）", "已达到20秒上限，发言已确认", "20초 상한에서 발화 확정", "Intervención confirmada al alcanzar el límite de 20 segundos", "Parole confirmée à la limite de 20 secondes", "Äußerung beim 20-Sekunden-Limit bestätigt", "Intervento confermato al limite di 20 secondi", "Fala confirmada no limite de 20 segundos"],
    detail_understanding: ["Silence detected. Understanding your speech", "無音を検出。発話を理解しています", "检测到静音，正在理解你的话", "무음 감지. 발화를 이해하는 중", "Silencio detectado. Comprendiendo lo que has dicho", "Silence détecté. Compréhension de votre parole", "Stille erkannt. Sprache wird verarbeitet", "Silenzio rilevato. Comprensione del parlato", "Silêncio detetado. A compreender a fala"],
    detail_responding: ["Generating a response", "応答を生成しています", "正在生成回复", "응답을 생성하는 중", "Generando una respuesta", "Génération d’une réponse", "Antwort wird erstellt", "Generazione di una risposta", "A gerar uma resposta"],
    detail_assistant_speaking: ["Reachy is speaking", "Reachyが話しています", "Reachy正在说话", "Reachy가 말하고 있습니다", "Reachy está hablando", "Reachy parle", "Reachy spricht", "Reachy sta parlando", "O Reachy está a falar"],
    detail_assistant_interruptible: ["Reachy is speaking (you can interrupt)", "Reachyが話しています（割り込み可能）", "Reachy正在说话（可以打断）", "Reachy가 말하는 중(끼어들기 가능)", "Reachy está hablando (puedes interrumpir)", "Reachy parle (vous pouvez l’interrompre)", "Reachy spricht (du kannst unterbrechen)", "Reachy sta parlando (puoi interrompere)", "O Reachy está a falar (pode interromper)"],
    detail_disconnected: ["Realtime connection lost", "Realtime接続が切れました", "Realtime连接已断开", "Realtime 연결이 끊어졌습니다", "Se perdió la conexión Realtime", "Connexion Realtime interrompue", "Realtime-Verbindung unterbrochen", "Connessione Realtime interrotta", "A ligação Realtime foi interrompida"],
    detail_error: ["An error occurred in the connection or audio processing", "接続または音声処理でエラーが発生しました", "连接或音频处理发生错误", "연결 또는 오디오 처리 중 오류 발생", "Se produjo un error en la conexión o el procesamiento de audio", "Une erreur s’est produite dans la connexion ou le traitement audio", "Bei der Verbindung oder Audioverarbeitung ist ein Fehler aufgetreten", "Si è verificato un errore nella connessione o nell’elaborazione audio", "Ocorreu um erro na ligação ou no processamento de áudio"],
    detail_stopped: ["The app has stopped", "アプリを停止しました", "应用已停止", "앱이 중지되었습니다", "La aplicación se ha detenido", "L’application est arrêtée", "Die App wurde gestoppt", "L’app è stata arrestata", "A aplicação parou"],
    no_status_detail: ["No status details", "状態詳細はありません", "无状态详情", "상태 세부 정보 없음", "No hay detalles de estado", "Aucun détail d’état", "Keine Statusdetails", "Nessun dettaglio sullo stato", "Sem detalhes de estado"],

    event_app_started: ["App started", "アプリを起動しました", "应用已启动", "앱 시작됨", "Aplicación iniciada", "Application démarrée", "App gestartet", "App avviata", "Aplicação iniciada"],
    event_usage_recorded: ["Usage: +{tokens} tokens · est. {cost} · cumulative {total}", "API使用量: +{tokens}トークン・推定 {cost}・累計 {total}", "API用量：+{tokens}令牌 · 估算 {cost} · 累计 {total}", "API 사용량: +{tokens}토큰 · 추정 {cost} · 누적 {total}", "Uso: +{tokens} tokens · est. {cost} · acumulado {total}", "Utilisation : +{tokens} jetons · est. {cost} · cumul {total}", "Nutzung: +{tokens} Tokens · geschätzt {cost} · kumuliert {total}", "Utilizzo: +{tokens} token · stima {cost} · cumulativo {total}", "Utilização: +{tokens} tokens · estimativa {cost} · acumulado {total}"],
    event_usage_unpriced: ["Usage: +{tokens} tokens · no price configured for {model}", "API使用量: +{tokens}トークン・{model}の料金設定なし", "API用量：+{tokens}令牌 · 未配置{model}的价格", "API 사용량: +{tokens}토큰 · {model} 가격 미설정", "Uso: +{tokens} tokens · sin precio configurado para {model}", "Utilisation : +{tokens} jetons · aucun tarif configuré pour {model}", "Nutzung: +{tokens} Tokens · kein Preis für {model} hinterlegt", "Utilizzo: +{tokens} token · nessun prezzo configurato per {model}", "Utilização: +{tokens} tokens · sem preço configurado para {model}"],
    event_camera_detected: ["Camera detected (AI camera is off by default)", "カメラを検出しました（AIカメラは初期OFF）", "检测到摄像头（AI摄像头默认关闭）", "카메라 감지됨(AI 카메라는 기본적으로 꺼짐)", "Cámara detectada (la cámara con IA está desactivada de forma predeterminada)", "Caméra détectée (la caméra IA est désactivée par défaut)", "Kamera erkannt (KI-Kamera ist standardmäßig aus)", "Fotocamera rilevata (la fotocamera IA è disattivata per impostazione predefinita)", "Câmara detetada (a câmara com IA está desligada por predefinição)"],
    event_camera_unavailable: ["Camera is unavailable", "カメラは利用できません", "摄像头不可用", "카메라를 사용할 수 없음", "La cámara no está disponible", "La caméra est indisponible", "Kamera ist nicht verfügbar", "La fotocamera non è disponibile", "A câmara não está disponível"],
    event_mic_config_applied: ["Applied the Reachy conversation microphone settings", "Reachy会話用のマイク設定を適用しました", "已应用Reachy对话麦克风设置", "Reachy 대화용 마이크 설정 적용됨", "Se aplicó la configuración de micrófono para conversación de Reachy", "Paramètres du microphone de conversation Reachy appliqués", "Reachy-Gesprächsmikrofon-Einstellungen angewendet", "Impostazioni microfono per la conversazione Reachy applicate", "Definições do microfone de conversa do Reachy aplicadas"],
    event_mic_config_current: ["Starting with the current microphone settings", "現在のマイク設定で開始します", "使用当前麦克风设置启动", "현재 마이크 설정으로 시작", "Iniciando con la configuración actual del micrófono", "Démarrage avec les paramètres actuels du microphone", "Start mit den aktuellen Mikrofoneinstellungen", "Avvio con le impostazioni attuali del microfono", "A iniciar com as definições atuais do microfone"],
    event_motion_catalog_unavailable: ["Could not load the recorded-move catalog: {dataset}", "収録モーションのカタログを読み込めていません: {dataset}", "无法加载录制动作库：{dataset}", "녹화된 모션 카탈로그를 불러오지 못했습니다: {dataset}", "No se pudo cargar el catálogo de movimientos grabados: {dataset}", "Impossible de charger le catalogue de mouvements enregistrés : {dataset}", "Katalog aufgezeichneter Bewegungen konnte nicht geladen werden: {dataset}", "Impossibile caricare il catalogo dei movimenti registrati: {dataset}", "Não foi possível carregar o catálogo de movimentos gravados: {dataset}"],
    event_mic_started: ["Microphone input started ({rate} Hz)", "マイク入力を開始しました（{rate} Hz）", "麦克风输入已启动（{rate} Hz）", "마이크 입력 시작됨({rate} Hz)", "Entrada del micrófono iniciada ({rate} Hz)", "Entrée microphone démarrée ({rate} Hz)", "Mikrofoneingang gestartet ({rate} Hz)", "Ingresso microfono avviato ({rate} Hz)", "Entrada do microfone iniciada ({rate} Hz)"],
    event_signal_detected: ["Microphone signal detected (ch{channel}: {dbfs} dBFS)", "マイクの音声信号を検出しました（ch{channel}: {dbfs} dBFS）", "检测到麦克风信号（ch{channel}: {dbfs} dBFS）", "마이크 신호 감지됨(ch{channel}: {dbfs} dBFS)", "Señal de micrófono detectada (ch{channel}: {dbfs} dBFS)", "Signal microphone détecté (ch{channel} : {dbfs} dBFS)", "Mikrofonsignal erkannt (ch{channel}: {dbfs} dBFS)", "Segnale microfono rilevato (ch{channel}: {dbfs} dBFS)", "Sinal do microfone detetado (ch{channel}: {dbfs} dBFS)"],
    event_local_speech_started: ["Local speech detection: started ({dbfs} dBFS / threshold {threshold} dBFS)", "ローカル音声判定: 発話開始（{dbfs} dBFS / 閾値 {threshold} dBFS）", "本地语音检测：开始（{dbfs} dBFS / 阈值 {threshold} dBFS）", "로컬 음성 감지: 발화 시작({dbfs} dBFS / 임계값 {threshold} dBFS)", "Detección local de voz: inicio ({dbfs} dBFS / umbral {threshold} dBFS)", "Détection vocale locale : début ({dbfs} dBFS / seuil {threshold} dBFS)", "Lokale Spracherkennung: Start ({dbfs} dBFS / Schwelle {threshold} dBFS)", "Rilevamento vocale locale: inizio ({dbfs} dBFS / soglia {threshold} dBFS)", "Deteção de voz local: início ({dbfs} dBFS / limiar {threshold} dBFS)"],
    event_camera_sending: ["Speech detected. Sending camera image to OpenAI", "発話を検知。カメラ画像をOpenAIへ送信しています", "检测到发言，正在向OpenAI发送摄像头图像", "발화 감지. 카메라 이미지를 OpenAI로 전송 중", "Voz detectada. Enviando imagen de la cámara a OpenAI", "Parole détectée. Envoi de l’image caméra à OpenAI", "Sprache erkannt. Kamerabild wird an OpenAI gesendet", "Voce rilevata. Invio dell’immagine della fotocamera a OpenAI", "Fala detetada. A enviar a imagem da câmara para a OpenAI"],
    event_camera_sent: ["Sent camera image at speech start to OpenAI ({size} KiB)", "発話開始時のカメラ画像をOpenAIへ送信しました（{size} KiB）", "已向OpenAI发送发言开始时的摄像头图像（{size} KiB）", "발화 시작 시 카메라 이미지를 OpenAI로 전송함({size} KiB)", "Imagen de cámara al inicio de la voz enviada a OpenAI ({size} KiB)", "Image caméra au début de la parole envoyée à OpenAI ({size} Kio)", "Kamerabild bei Sprechbeginn an OpenAI gesendet ({size} KiB)", "Immagine della fotocamera all’inizio del parlato inviata a OpenAI ({size} KiB)", "Imagem da câmara no início da fala enviada para a OpenAI ({size} KiB)"],
    event_camera_on: ["AI camera enabled (sends an image when speech starts)", "AIカメラをONにしました（発話開始時に画像をOpenAIへ送信）", "AI摄像头已开启（发言开始时向OpenAI发送图像）", "AI 카메라 켜짐(발화 시작 시 OpenAI로 이미지 전송)", "Cámara con IA activada (envía una imagen al empezar a hablar)", "Caméra IA activée (envoi d’une image au début de la parole)", "KI-Kamera aktiviert (Bildversand bei Sprechbeginn)", "Fotocamera IA attivata (invia un’immagine all’inizio del parlato)", "Câmara com IA ativada (envia uma imagem ao começar a falar)"],
    event_camera_off: ["AI camera disabled", "AIカメラをOFFにしました", "AI摄像头已关闭", "AI 카메라 꺼짐", "Cámara con IA desactivada", "Caméra IA désactivée", "KI-Kamera deaktiviert", "Fotocamera IA disattivata", "Câmara com IA desativada"],
    event_interruption: ["Interruption: stopped Reachy's speech", "割り込み: Reachyの発話を停止しました", "打断：已停止Reachy说话", "끼어들기: Reachy의 발화 중지", "Interrupción: se detuvo el habla de Reachy", "Interruption : parole de Reachy arrêtée", "Unterbrechung: Reachys Sprache gestoppt", "Interruzione: parlato di Reachy arrestato", "Interrupção: fala do Reachy parada"],
    event_interruption_played: ["Interruption: stopped Reachy's speech ({ms} ms played)", "割り込み: Reachyの発話を停止しました（再生済み {ms}ms）", "打断：已停止Reachy说话（已播放 {ms}毫秒）", "끼어들기: Reachy의 발화 중지({ms}ms 재생됨)", "Interrupción: se detuvo el habla de Reachy ({ms} ms reproducidos)", "Interruption : parole de Reachy arrêtée ({ms} ms lus)", "Unterbrechung: Reachys Sprache gestoppt ({ms} ms abgespielt)", "Interruzione: parlato di Reachy arrestato ({ms} ms riprodotti)", "Interrupção: fala do Reachy parada ({ms} ms reproduzidos)"],
    event_user_transcript: ["You: {text}", "あなた: {text}", "你：{text}", "나: {text}", "Tú: {text}", "Vous : {text}", "Du: {text}", "Tu: {text}", "Tu: {text}"],
    event_assistant_transcript: ["Reachy: {text}", "Reachy: {text}", "Reachy：{text}", "Reachy: {text}", "Reachy: {text}", "Reachy : {text}", "Reachy: {text}", "Reachy: {text}", "Reachy: {text}"],
    event_system: ["System event", "システムイベント", "系统事件", "시스템 이벤트", "Evento del sistema", "Événement système", "Systemereignis", "Evento di sistema", "Evento do sistema"],

    language_save_failed: ["Could not save the language setting", "言語設定を保存できませんでした", "无法保存语言设置", "언어 설정을 저장할 수 없습니다", "No se pudo guardar el idioma", "Impossible d’enregistrer la langue", "Spracheinstellung konnte nicht gespeichert werden", "Impossibile salvare la lingua", "Não foi possível guardar o idioma"],
    language_changed: ["Changed to {language}. The UI is now updated, and conversation changes apply from the next response.", "{language}に変更しました。画面は切り替わり、会話は次の応答から反映されます。", "已切换为{language}。界面已更新，对话设置将从下一次回复开始生效。", "{language}(으)로 변경했습니다. 화면은 즉시 바뀌며 대화는 다음 응답부터 적용됩니다.", "Idioma cambiado a {language}. La interfaz ya se ha actualizado y la conversación cambiará desde la siguiente respuesta.", "Langue changée en {language}. L’interface est mise à jour et la conversation changera dès la prochaine réponse.", "Auf {language} umgestellt. Die Oberfläche ist aktualisiert; das Gespräch wechselt ab der nächsten Antwort.", "Lingua cambiata in {language}. L’interfaccia è aggiornata e la conversazione cambierà dalla prossima risposta.", "Idioma alterado para {language}. A interface já foi atualizada e a conversa muda a partir da próxima resposta."],
    robot_status_failed: ["Could not get the robot status", "ロボットの状態を取得できませんでした", "无法获取机器人状态", "로봇 상태를 가져올 수 없습니다", "No se pudo obtener el estado del robot", "Impossible d’obtenir l’état du robot", "Roboterstatus konnte nicht abgerufen werden", "Impossibile ottenere lo stato del robot", "Não foi possível obter o estado do robô"],
    saving: ["Saving…", "保存中…", "正在保存…", "저장 중…", "Guardando…", "Enregistrement…", "Wird gespeichert…", "Salvataggio…", "A guardar…"],
    key_save_failed: ["Could not save the API key", "APIキーを保存できませんでした", "无法保存API密钥", "API 키를 저장할 수 없습니다", "No se pudo guardar la clave API", "Impossible d’enregistrer la clé API", "API-Schlüssel konnte nicht gespeichert werden", "Impossibile salvare la chiave API", "Não foi possível guardar a chave API"],
    saved_restart: ["Saved. Restart the app.", "保存しました。アプリを再起動してください。", "已保存。请重启应用。", "저장했습니다. 앱을 다시 시작하세요.", "Guardado. Reinicia la aplicación.", "Enregistré. Redémarrez l’application.", "Gespeichert. Bitte die App neu starten.", "Salvato. Riavvia l’app.", "Guardado. Reinicie a aplicação."],
    saved_start: ["Saved. Starting the conversation.", "保存しました。会話を開始します。", "已保存。正在开始对话。", "저장했습니다. 대화를 시작합니다.", "Guardado. Iniciando la conversación.", "Enregistré. Démarrage de la conversation.", "Gespeichert. Das Gespräch wird gestartet.", "Salvato. Avvio della conversazione.", "Guardado. A iniciar a conversa."],
    confirm_remove: ["Remove the saved OpenAI API key?", "保存済みのOpenAI APIキーを削除しますか？", "要删除已保存的OpenAI API密钥吗？", "저장된 OpenAI API 키를 삭제할까요?", "¿Eliminar la clave API de OpenAI guardada?", "Supprimer la clé API OpenAI enregistrée ?", "Gespeicherten OpenAI-API-Schlüssel entfernen?", "Rimuovere la chiave API OpenAI salvata?", "Remover a chave API da OpenAI guardada?"],
    key_remove_failed: ["Could not remove the API key", "APIキーを削除できませんでした", "无法删除API密钥", "API 키를 삭제할 수 없습니다", "No se pudo eliminar la clave API", "Impossible de supprimer la clé API", "API-Schlüssel konnte nicht entfernt werden", "Impossibile rimuovere la chiave API", "Não foi possível remover a chave API"],
    removed_restart: ["Removed. Restart the app.", "削除しました。アプリを再起動してください。", "已删除。请重启应用。", "삭제했습니다. 앱을 다시 시작하세요.", "Eliminada. Reinicia la aplicación.", "Supprimée. Redémarrez l’application.", "Entfernt. Bitte die App neu starten.", "Rimossa. Riavvia l’app.", "Removida. Reinicie a aplicação."],
    removed: ["Removed.", "削除しました。", "已删除。", "삭제했습니다.", "Eliminada.", "Supprimée.", "Entfernt.", "Rimossa.", "Removida."],
    camera_setting_failed: ["Could not change the camera setting", "カメラ設定を変更できませんでした", "无法更改摄像头设置", "카메라 설정을 변경할 수 없습니다", "No se pudo cambiar la configuración de la cámara", "Impossible de modifier le réglage de la caméra", "Kameraeinstellung konnte nicht geändert werden", "Impossibile modificare l’impostazione della fotocamera", "Não foi possível alterar a definição da câmara"],
    camera_enabled_message: ["AI camera enabled. A still image will be sent to OpenAI when the next speech starts.", "AIカメラをONにしました。次の発話開始時に静止画をOpenAIへ送信します。", "AI摄像头已开启。下次发言开始时会向OpenAI发送一张静止图像。", "AI 카메라를 켰습니다. 다음 발화 시작 시 정지 이미지를 OpenAI로 전송합니다.", "Cámara con IA activada. Se enviará una imagen fija a OpenAI al empezar a hablar.", "Caméra IA activée. Une image fixe sera envoyée à OpenAI au prochain début de parole.", "KI-Kamera aktiviert. Beim nächsten Sprechbeginn wird ein Standbild an OpenAI gesendet.", "Fotocamera IA attivata. Al prossimo inizio del parlato verrà inviata un’immagine fissa a OpenAI.", "Câmara com IA ativada. Uma imagem fixa será enviada para a OpenAI no início da próxima fala."],
    camera_disabled_message: ["AI camera disabled.", "AIカメラをOFFにしました。", "AI摄像头已关闭。", "AI 카메라를 껐습니다.", "Cámara con IA desactivada.", "Caméra IA désactivée.", "KI-Kamera deaktiviert.", "Fotocamera IA disattivata.", "Câmara com IA desativada."],
    diagnostics_failed: ["Could not get the diagnostics", "診断ログを取得できませんでした", "无法获取诊断信息", "진단 로그를 가져올 수 없습니다", "No se pudo obtener el diagnóstico", "Impossible d’obtenir le diagnostic", "Diagnose konnte nicht abgerufen werden", "Impossibile ottenere la diagnostica", "Não foi possível obter o diagnóstico"],
    diagnostics_copied: ["Diagnostics copied.", "診断ログをコピーしました。", "诊断信息已复制。", "진단 로그를 복사했습니다.", "Diagnóstico copiado.", "Diagnostic copié.", "Diagnose kopiert.", "Diagnostica copiata.", "Diagnóstico copiado."],
  };

  const strings = Object.fromEntries(CODES.map((code, index) => [
    code,
    Object.fromEntries(Object.entries(rows).map(([key, values]) => [key, values[index]])),
  ]));

  function language(code) {
    return Object.hasOwn(strings, code) ? code : "en";
  }

  function t(code, key, params = {}) {
    const resolved = language(code);
    const template = strings[resolved][key] ?? strings.en[key] ?? key;
    return template.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? `{${name}}`));
  }

  const legacyExact = {
    "アプリを起動しています": "detail_starting",
    "OpenAI APIキーを設定してください": "detail_waiting_key",
    "マイクとスピーカーを準備しています": "detail_starting_audio",
    "Wirelessマイクを調整しています": "detail_tuning_audio",
    "Realtime APIへ接続しています": "detail_connecting",
    "Realtimeセッションを再起動しています": "detail_reconnecting",
    "音声を聞いています": "detail_user_speaking",
    "発話を確定しました（無音800ms）": "detail_turn_silence",
    "発話を確定しました（発話上限20秒）": "detail_turn_maximum",
    "無音を検出。発話を理解しています": "detail_understanding",
    "応答を生成しています": "detail_responding",
    "Reachyが話しています": "detail_assistant_speaking",
    "Reachyが話しています（割り込み可能）": "detail_assistant_interruptible",
    "Realtime接続が切れました": "detail_disconnected",
    "接続または音声処理でエラーが発生しました": "detail_error",
    "停止しました": "detail_stopped",
    "アプリを停止しました": "detail_stopped",
    "アプリを起動しました": "event_app_started",
    "カメラを検出しました（AIカメラは初期OFF）": "event_camera_detected",
    "カメラは利用できません": "event_camera_unavailable",
    "Reachy会話用のマイク設定を適用しました": "event_mic_config_applied",
    "現在のマイク設定で開始します": "event_mic_config_current",
    "発話を検知。カメラ画像をOpenAIへ送信しています": "event_camera_sending",
    "AIカメラをONにしました（発話開始時に画像をOpenAIへ送信）": "event_camera_on",
    "AIカメラをOFFにしました": "event_camera_off",
    "割り込み: Reachyの発話を停止しました": "event_interruption",
  };

  function translateLegacy(code, message, phase = "") {
    if (!message) return "";
    const exactKey = legacyExact[message];
    if (exactKey) return t(code, exactKey);
    let match = message.match(/^マイク入力を開始しました（(\d+) Hz）$/);
    if (match) return t(code, "event_mic_started", { rate: match[1] });
    match = message.match(/^マイクの音声信号を検出しました（ch(\d+): ([\d.-]+) dBFS）$/);
    if (match) return t(code, "event_signal_detected", { channel: match[1], dbfs: match[2] });
    match = message.match(/^ローカル音声判定: 発話開始\s*（([\d.-]+) dBFS \/ 閾値 ([\d.-]+) dBFS）$/);
    if (match) return t(code, "event_local_speech_started", { dbfs: match[1], threshold: match[2] });
    match = message.match(/^発話開始時のカメラ画像をOpenAIへ送信しました（(\d+) KiB）$/);
    if (match) return t(code, "event_camera_sent", { size: match[1] });
    match = message.match(/^あなた: (.*)$/s);
    if (match) return t(code, "event_user_transcript", { text: match[1] });
    match = message.match(/^Reachy: (.*)$/s);
    if (match) return t(code, "event_assistant_transcript", { text: match[1] });
    match = message.match(/^(?:接続済み。)?(.+)で話しかけてください/);
    if (match) return t(code, message.startsWith("接続済み。") ? "detail_listening_connected" : "detail_listening", { language: match[1] });
    const phaseKey = `detail_${phase}`;
    if (/[\u3040-\u30ff]/.test(message) && strings[language(code)][phaseKey]) return t(code, phaseKey);
    if (/[\u3040-\u30ff]/.test(message) && code !== "ja") return t(code, "event_system");
    return message;
  }

  window.ReachyI18n = {
    codes: CODES,
    localeFor: (code) => LOCALES[language(code)],
    t,
    translateRuntime(code, message, key, params = {}, phase = "") {
      return key ? t(code, key, params) : translateLegacy(code, message, phase);
    },
  };
})();
