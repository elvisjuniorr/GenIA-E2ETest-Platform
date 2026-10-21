(() => {
  const root = (window.GenIA = window.GenIA || {});
  const { API_BASE_URL } = root.constants;

  class GenIAAPI {
    constructor(baseURL = API_BASE_URL) {
      this.baseURL = baseURL;
      this.currentLLMConfig = null;
      this.activeLogStream = null;
    }

    log(message, details = null) {
      if (details !== null && details !== undefined) {
        console.info(`[GenIAAPI] ${message}`, details);
      } else {
        console.info(`[GenIAAPI] ${message}`);
      }
    }

    async request(endpoint, method = "GET", body = null) {
      try {
        const url = `${this.baseURL}${endpoint}`;
        const options = {
          method,
          mode: "cors",
          headers: {},
        };

        if (method === "GET") {
          options.headers.Accept = "application/json";
        }

        if (body && (method === "POST" || method === "PUT" || method === "PATCH")) {
          options.headers["Content-Type"] = "application/json";
          options.body = JSON.stringify(body);
        }

        const response = await fetch(url, options);
        const contentType = response.headers.get("content-type") || "";
        const data = contentType.includes("application/json") ? await response.json() : await response.text();

        if (!response.ok) {
          if (typeof data === "string") {
            throw new Error(`${data.slice(0, 300)}${data.length > 300 ? "..." : ""}`);
          }
          throw new Error(data.error || `HTTP ${response.status}`);
        }

        return {
          success: true,
          status: response.status,
          data,
        };
      } catch (error) {
        console.error(`API Error [${endpoint}]`, error);
        return {
          success: false,
          error: error.message,
        };
      }
    }

    get(endpoint) {
      return this.request(endpoint, "GET");
    }

    post(endpoint, body) {
      return this.request(endpoint, "POST", body);
    }

    async submitPipeline(endpoint, body, onStatus = null) {
      const submitted = await this.post(endpoint, body);
      if (!submitted.success || submitted.status !== 202) return submitted;

      const pipelineId = submitted.data?.pipeline_id || body.pipeline_id;
      if (!pipelineId) return { success: false, error: "O servidor não retornou o ID da pipeline." };
      onStatus?.("queued", submitted.data);

      while (true) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        const statusResponse = await this.get(`/pipeline/status/${encodeURIComponent(pipelineId)}`);
        if (!statusResponse.success) return statusResponse;
        const current = statusResponse.data?.status;
        onStatus?.(current, statusResponse.data);

        if (["WAITING_VALIDATION", "COMPLETED", "ERROR"].includes(current)) {
          return {
            success: current !== "ERROR",
            status: 200,
            error: statusResponse.data.result?.error,
            data: statusResponse.data.result || { status: current === "ERROR" ? "error" : "completed", pipeline_id: pipelineId },
          };
        }
        if (current === "missing") return { success: false, error: "A sessão da pipeline expirou. Inicie novamente." };
      }
    }

    healthCheck() {
      return this.get("/health").then((response) => response.data);
    }

    getPlatformInfo() {
      return this.get("/info").then((response) => response.data);
    }

    getFrameworks() {
      return this.get("/frameworks").then((response) => response.data);
    }

    getLLMModels() {
      return this.get("/llm/models").then((response) => response.data);
    }

    getPromptBundle(framework) {
      const query = framework ? `?framework=${encodeURIComponent(framework)}` : "";
      return this.get(`/pipeline/prompts${query}`).then((response) => response.data);
    }

    validateLLMConnection(provider, model, apiKey) {
      return this.post("/llm/validate", {
        provider,
        model,
        api_key: apiKey,
      }).then((response) => {
        if (response.success) {
          this.currentLLMConfig = {
            provider,
            model,
            api_key: apiKey,
          };
        }
        return response;
      });
    }

    initializePipeline(provider, model, apiKey) {
      return this.post("/pipeline/initialize", {
        provider,
        model,
        api_key: apiKey,
      }).then((response) => {
        if (response.success) {
          this.currentLLMConfig = {
            provider,
            model,
            api_key: apiKey,
          };
        }
        return response;
      });
    }

    restructure(testCase, urls, project, mode) {
      return this.post("/pipeline/restructure", {
        test_case: testCase,
        urls,
        project,
        mode,
      });
    }

    extract(structuredJson, urls, headless = true) {
      return this.post("/pipeline/extract", {
        structured_json: structuredJson,
        urls,
        headless,
      });
    }

    refine(extractedJson, structuredJson, urls, headless = true) {
      return this.post("/pipeline/refine", {
        extracted_json: extractedJson,
        structured_json: structuredJson,
        urls,
        headless,
      });
    }

    generateScript(refinedJson, framework, language, project) {
      return this.post("/pipeline/generate-script", {
        refined_json: refinedJson,
        framework,
        language,
        project,
      });
    }

    validate(script, refinedJson, framework, language) {
      return this.post("/pipeline/validate", {
        script,
        refined_json: refinedJson,
        framework,
        language,
      });
    }

    runFullPipeline1(
      testCase,
      framework,
      language,
      provider,
      model,
      apiKey,
      testName = "untitled",
      headless = true,
      attempt = 1,
      pipelineId = null,
      promptOverrides = null,
      onStatus = null
    ) {
      const payload = {
        test_case: testCase,
        framework,
        language,
        provider,
        model,
        api_key: apiKey,
        test_name: testName,
        headless,
        attempt,
        pipeline_id: pipelineId || (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`),
        prompt_overrides: promptOverrides || undefined,
      };
      return this.submitPipeline("/pipeline/run-until-validation", payload, onStatus);
    }

    runFullPipeline(
      testCase,
      framework,
      language,
      provider,
      model,
      apiKey,
      testName = "untitled",
      headless = true,
      attempt = 1,
      pipelineId = null,
      promptOverrides = null,
      llmOverrides = null,
      onStatus = null
    ) {
      const payload = {
        test_case: testCase,
        framework,
        language,
        provider,
        model,
        api_key: apiKey,
        test_name: testName,
        headless,
        attempt,
        pipeline_id: pipelineId || (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`),
        prompt_overrides: promptOverrides || undefined,
        llm_overrides: llmOverrides || undefined,
      };
      return this.submitPipeline("/pipeline/run-until-validation", payload, onStatus);
    }

    runFullPipeline2(
      pipelineId,
      manualChanges = null,
      continueWithoutChanges = true,
      resumeFromStage = "confirmation",
      scriptOverride = null,
      promptOverrides = null,
      llmOverrides = null,
      onStatus = null,
      visualizeExecution = false,
      llmConfig = null,
      sessionContext = null
    ) {
      const payload = {
        pipeline_id: pipelineId,
        manual_changes: manualChanges,
        continue_without_changes: continueWithoutChanges,
        resume_from_stage: resumeFromStage,
        script_override: scriptOverride || undefined,
        prompt_overrides: promptOverrides || undefined,
        llm_overrides: llmOverrides || undefined,
        visualize_execution: Boolean(visualizeExecution),
        llm_config: llmConfig || undefined,
        session_context: sessionContext || undefined,
      };
      return this.submitPipeline("/pipeline/continue-after-validation", payload, onStatus);
    }

    continueAfterValidation(
      pipelineId,
      manualChanges = null,
      continueWithoutChanges = true,
      resumeFromStage = "confirmation",
      scriptOverride = null,
      promptOverrides = null,
      llmOverrides = null
    ) {
      return this.runFullPipeline2(
        pipelineId,
        manualChanges,
        continueWithoutChanges,
        resumeFromStage,
        scriptOverride,
        promptOverrides,
        llmOverrides
      );
    }

    getLogs() {
      return this.get("/logs").then((response) => response.data);
    }

    streamPipelineLogs(pipelineId, handlers = {}) {
      if (!pipelineId || typeof EventSource === "undefined") {
        return null;
      }

      if (this.activeLogStream) {
        this.activeLogStream.close();
      }

      const source = new EventSource(`${this.baseURL}/pipeline/logs/stream/${pipelineId}`);
      this.activeLogStream = source;

      source.onmessage = (event) => {
        if (!event.data) return;
        try {
          const payload = JSON.parse(event.data);
          handlers.onLog?.(payload);
        } catch (error) {
          handlers.onError?.(error);
        }
      };

      source.addEventListener("done", () => {
        handlers.onDone?.();
        source.close();
        if (this.activeLogStream === source) {
          this.activeLogStream = null;
        }
      });

      source.onerror = (error) => {
        handlers.onError?.(error);
      };

      return source;
    }

    closePipelineLogStream() {
      if (this.activeLogStream) {
        this.activeLogStream.close();
        this.activeLogStream = null;
      }
    }

    clearLogs() {
      return this.post("/logs/clear", {});
    }

    getCurrentLLMConfig() {
      return this.currentLLMConfig;
    }

    isLLMConfigured() {
      return this.currentLLMConfig !== null;
    }

    clearLLMConfig() {
      this.currentLLMConfig = null;
    }

    getProviderName() {
      return this.currentLLMConfig?.provider || null;
    }

    getModelName() {
      return this.currentLLMConfig?.model || null;
    }

    getLanguagesForFramework(framework) {
      return root.constants.FRAMEWORK_LANGUAGES[framework] || [];
    }

    isValidFramework(framework) {
      return ["robotframework", "playwright", "cypress", "pytest", "junit"].includes(framework);
    }

    isValidProvider(provider) {
      return ["openai", "anthropic", "gemini", "cohere", "claude"].includes(provider);
    }

    buildModulePayload(url, purpose, executionSteps) {
      return {
        url,
        purpose,
        execution_steps: executionSteps.map((step) => ({
          step,
          extracted_data: [],
        })),
      };
    }
  }

  root.api = {
    GenIAAPI,
    geniaAPI: new GenIAAPI(),
  };
})();
