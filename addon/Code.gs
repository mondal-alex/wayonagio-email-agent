/**
 * Wayonagio Gmail Add-on
 *
 * Adds language-specific draft buttons when viewing an email in Gmail.
 * Clicking a button calls POST /generate-reply on the backend server, then
 * opens the generated reply with Gmail's native compose UI.
 *
 * Setup (run once per deployment):
 *   1. Open this project in Apps Script editor.
 *   2. Go to Project Settings → Script Properties and add:
 *        BACKEND_URL   — e.g. https://your-server.example.com
 *        BEARER_TOKEN  — same value as AUTH_BEARER_TOKEN in your .env
 *   3. In appsscript.json, set urlFetchWhitelist to include your BACKEND_URL host
 *        (e.g. https://*.a.run.app/ for Cloud Run). Required for UrlFetchApp.
 *   4. Deploy as a Google Workspace Add-on and install for your domain.
 */

/**
 * Builds the Add-on card shown when an email is open.
 * @param {Object} e - Gmail contextual event object.
 * @returns {Card}
 */
function buildAddOn(e) {
  var messageId = e.gmail.messageId;
  var threadId = e.gmail.threadId;

  return buildDraftButtonsCard(messageId, threadId);
}

/**
 * Builds the initial card with the two language draft buttons.
 * @param {string} messageId
 * @param {string} threadId
 * @returns {Card}
 */
function buildDraftButtonsCard(messageId, threadId) {
  var italianButton = CardService.newTextButton()
    .setText("🇮🇹 Borrador en italiano")
    .setComposeAction(
      CardService.newAction()
        .setFunctionName("onGenerateReplyDraft")
        .setParameters({
          messageId: messageId,
          threadId: threadId,
          language: "it",
        }),
      CardService.ComposedEmailType.REPLY_AS_DRAFT
    );

  var spanishButton = CardService.newTextButton()
    .setText("🇵🇪 Borrador en espanol")
    .setComposeAction(
      CardService.newAction()
        .setFunctionName("onGenerateReplyDraft")
        .setParameters({
          messageId: messageId,
          threadId: threadId,
          language: "es",
        }),
      CardService.ComposedEmailType.REPLY_AS_DRAFT
    );

  var section = CardService.newCardSection()
    .setHeader("Wayonagio")
    .addWidget(italianButton)
    .addWidget(spanishButton);

  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle("Borrador de respuesta"))
    .addSection(section)
    .build();
}

/**
 * Extract a user-facing error message from a backend response body.
 * @param {string} text
 * @returns {string}
 */
function parseErrorMessage(text) {
  try {
    var body = JSON.parse(text);
    if (body && body.detail) {
      return String(body.detail);
    }
  } catch (err) {
    // Fall through to raw body below.
  }
  return text || "No se pudo crear el borrador.";
}

/**
 * Called when the user clicks a language button. POSTs to the backend for
 * generated text, then returns a Gmail draft that Gmail opens in compose UI.
 * @param {Object} e - Action event with parameters.messageId, threadId, language.
 * @returns {ComposeActionResponse}
 */
function onGenerateReplyDraft(e) {
  var messageId = (e.gmail && e.gmail.messageId) || e.parameters.messageId;
  var threadId = (e.gmail && e.gmail.threadId) || e.parameters.threadId;
  var language = e.parameters.language;
  var props = PropertiesService.getScriptProperties();
  var backendUrl = (props.getProperty("BACKEND_URL") || "").trim();
  var bearerToken = (props.getProperty("BEARER_TOKEN") || "").trim();

  if (!backendUrl || !bearerToken) {
    throw new Error(
      "Complemento no configurado. Define BACKEND_URL y BEARER_TOKEN en Propiedades del script."
    );
  }

  if (!e.gmail || !e.gmail.accessToken) {
    throw new Error("No se pudo autorizar Gmail para crear el borrador.");
  }

  var url = backendUrl.replace(/\/$/, "") + "/generate-reply";

  var options = {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + bearerToken },
    payload: JSON.stringify({
      message_id: messageId,
      thread_id: threadId,
      language: language,
    }),
    muteHttpExceptions: true,
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var code = response.getResponseCode();

    if (code === 200) {
      var body = JSON.parse(response.getContentText());
      var replyBody = body.body;
      var anchorMessageId = body.anchor_message_id;

      if (!replyBody) {
        throw new Error("El servidor no devolvio texto para el borrador.");
      }
      if (!anchorMessageId) {
        throw new Error("El servidor no devolvio el mensaje ancla para el borrador.");
      }

      GmailApp.setCurrentMessageAccessToken(e.gmail.accessToken);
      var draft = GmailApp.getMessageById(anchorMessageId).createDraftReply(replyBody);

      return CardService.newComposeActionResponseBuilder()
        .setGmailDraft(draft)
        .build();
    } else {
      var errorText = parseErrorMessage(response.getContentText());
      Logger.log("Backend error " + code + ": " + errorText);
      throw new Error(errorText);
    }
  } catch (err) {
    Logger.log("Request failed: " + err);
    throw err;
  }
}
