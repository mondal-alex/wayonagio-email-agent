/**
 * Wayonagio Gmail Add-on
 *
 * Adds language-specific draft buttons when viewing an email in Gmail.
 * Clicking a button calls POST /draft-reply on the backend server.
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
    .setOnClickAction(
      CardService.newAction()
        .setFunctionName("onDraftReply")
        .setParameters({
          messageId: messageId,
          threadId: threadId,
          language: "it",
        })
    );

  var spanishButton = CardService.newTextButton()
    .setText("🇵🇪 Borrador en espanol")
    .setOnClickAction(
      CardService.newAction()
        .setFunctionName("onDraftReply")
        .setParameters({
          messageId: messageId,
          threadId: threadId,
          language: "es",
        })
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
 * Builds the success card shown after a draft is created. Provides a button
 * that opens the Gmail thread so the user can see the new draft without
 * manually refreshing or navigating to the Drafts folder.
 * @param {string} draftId
 * @param {string} threadId
 * @param {string} messageId
 * @returns {Card}
 */
function buildSuccessCard(draftId, threadId, messageId) {
  var baseUrl = "https://mail.google.com/mail";
  var threadUrl = threadId
    ? baseUrl + "/#all/" + encodeURIComponent(threadId)
    : baseUrl + "/#drafts";

  var openThreadButton = CardService.newTextButton()
    .setText("Abrir hilo en una pestana nueva")
    .setOpenLink(
      CardService.newOpenLink()
        .setUrl(threadUrl)
        .setOpenAs(CardService.OpenAs.FULL_SIZE)
        .setOnClose(CardService.OnClose.NOTHING)
    );

  var backButton = CardService.newTextButton()
    .setText("Volver a los botones")
    .setOnClickAction(
      CardService.newAction()
        .setFunctionName("onBackToButtons")
        .setParameters({ messageId: messageId, threadId: threadId })
    );

  var section = CardService.newCardSection()
    .setHeader("Borrador creado")
    .addWidget(
      CardService.newTextParagraph().setText(
        "Tu borrador esta listo en este hilo. Haz clic abajo para abrirlo."
      )
    )
    .addWidget(openThreadButton)
    .addWidget(backButton)
    .addWidget(
      CardService.newTextParagraph().setText(
        "<font color=\"#888888\">ID de borrador: " + draftId + "</font>"
      )
    );

  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle("Wayonagio"))
    .addSection(section)
    .build();
}

/**
 * Called when the user clicks a "Draft in <language>" button.
 * POSTs to the backend, then swaps the card for a success card with a button
 * that opens the Gmail thread.
 * @param {Object} e - Action event with parameters.messageId, threadId, language.
 * @returns {ActionResponse}
 */
function onDraftReply(e) {
  var messageId = e.parameters.messageId;
  var threadId = e.parameters.threadId;
  var language = e.parameters.language;
  var props = PropertiesService.getScriptProperties();
  var backendUrl = (props.getProperty("BACKEND_URL") || "").trim();
  var bearerToken = (props.getProperty("BEARER_TOKEN") || "").trim();

  if (!backendUrl || !bearerToken) {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText(
          "Complemento no configurado. Define BACKEND_URL y BEARER_TOKEN en Propiedades del script."
        )
      )
      .build();
  }

  var url = backendUrl.replace(/\/$/, "") + "/draft-reply";

  var options = {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + bearerToken },
    payload: JSON.stringify({ message_id: messageId, language: language }),
    muteHttpExceptions: true,
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var code = response.getResponseCode();

    if (code === 200) {
      var body = JSON.parse(response.getContentText());
      var successCard = buildSuccessCard(body.draft_id, threadId, messageId);

      return CardService.newActionResponseBuilder()
        .setNotification(
          CardService.newNotification().setText("Borrador creado")
        )
        .setNavigation(CardService.newNavigation().updateCard(successCard))
        .build();
    } else {
      Logger.log("Backend error " + code + ": " + response.getContentText());
      return CardService.newActionResponseBuilder()
        .setNotification(
          CardService.newNotification().setText(
            "Error " + code + ": " + response.getContentText()
          )
        )
        .build();
    }
  } catch (err) {
    Logger.log("Request failed: " + err);
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText("La solicitud fallo: " + err)
      )
      .build();
  }
}

/**
 * Called from the success card's "Back to draft buttons" button.
 * Restores the initial card for the same message/thread.
 * @param {Object} e - Action event with parameters.messageId and threadId.
 * @returns {ActionResponse}
 */
function onBackToButtons(e) {
  var messageId = e.parameters.messageId;
  var threadId = e.parameters.threadId;
  var card = buildDraftButtonsCard(messageId, threadId);
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(card))
    .build();
}
