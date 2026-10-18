# GenIA-E2ETest: A Generative AI-Based Platform for End-to-End Test Automation

This repository contains the artifact accompanying the paper:

**GenIA-E2ETest: A Generative AI-Based Platform for End-to-End Test Automation**, accepted for publication at the **XL Brazilian Symposium on Software Engineering (SBES - Tools Track 2026)**.  

## 🔗 Link to the Accepted Paper

The full paper is available at:  
📄 [./GenIA-E2ETest-Platform.pdf](./GenIA-E2ETest-Platform.pdf)  

Authors:  
- Elvis Júnior (Universidade Federal Fluminense, Brazil)  
- Pedro Amaro (Universidade Federal Fluminense, Brazil)  
- Allber Ferreira (Universidade Federal Fluminense, Brazil)  
- Vânia de Oliveira Neves (Universidade Federal Fluminense, Brazil)

---

## 🧪 Artifact Overview

GenIA-E2ETest Platform is a full-stack artifact for generating, validating, executing, and refining end-to-end test scripts with generative AI. The platform includes:

- a web frontend for test configuration, prompt inspection, and result visualization;
- a Flask-based backend that exposes the pipeline API and orchestrates the generation flow;
- prompt templates for each pipeline stage;
- execution strategies for the supported framework/language combinations;
- automated tests covering the backend refactor and execution dispatch.

The artifact is self-contained and organized to support both reuse and reproducibility. The pipeline can generate tests from natural language inputs, continue after manual validation, and execute the produced scripts using the supported runner for each target framework and language.

- Demo video: https://youtu.be/-9xh_ThdfV4

---

## 📁 Repository Structure

```text
GenIA-E2ETest-Platform/
├── backend/                        # Flask API, pipeline orchestration, execution strategies, and backend tests
│   ├── api.py                      # Local backend entry point used to start the server in development
│   ├── app.py                      # Compatibility facade that exposes the Flask application object
│   ├── config.py                   # Centralized runtime configuration and environment resolution
│   ├── Aptfile                     # OS-level packages required by the backend container/runtime
│   ├── Dockerfile                  # Container image definition for deployment and reproducible runs
│   ├── requirements.txt            # Python dependencies required by the backend
│   ├── application/                # Application-level orchestration and prompt helpers
│   │   ├── __init__.py             # Package marker
│   │   ├── pipeline.py             # Public pipeline facade consumed by the HTTP layer
│   │   └── prompts.py              # Prompt selection and prompt-bundle helpers
│   ├── domain/                     # Core domain models and framework metadata
│   │   ├── __init__.py             # Package marker
│   │   ├── frameworks.py           # Framework/language catalog used by the application
│   │   └── models.py               # Shared domain and response data models
│   ├── http/                       # HTTP app factory, routes, and middleware wiring
│   │   ├── __init__.py             # Package marker
│   │   └── app_factory.py          # Flask app creation, routes, CORS, and socket wiring
│   ├── infrastructure/             # Execution engine, LLM adapters, and framework/language strategies
│   │   ├── __init__.py             # Package marker
│   │   ├── TestExecutor.py         # Dispatches execution to the correct framework/language strategy
│   │   ├── llm_factory.py          # Creates the correct LLM provider adapter
│   │   └── frameworks_languages/   # Local execution strategies per framework/language combination
│   │       ├── __init__.py         # Exposes the execution strategy registry
│   │       ├── base.py             # Shared execution helpers used by all strategies
│   │       ├── cypress.py          # Cypress execution strategies
│   │       ├── java_native.py      # Java/JUnit helper utilities and runner generation
│   │       ├── junit.py            # JUnit + Selenium execution strategy
│   │       ├── playwright.py       # Playwright execution strategies
│   │       ├── pytest.py           # Pytest + Selenium execution strategy
│   │       ├── registry.py         # Maps framework/language pairs to execution strategies
│   │       ├── robotframework.py   # Robot Framework + SeleniumLibrary execution strategy
│   │       ├── runtime.py          # Runtime translation helpers used by JavaScript-based runners
│   │       └── selenium.py         # Selenium execution strategies for supported languages
│   ├── llms/                       # Provider-specific LLM clients
│   │   ├── __init__.py             # Package marker
│   │   ├── anthropic.py            # Anthropic client integration
│   │   ├── cohere.py               # Cohere client integration
│   │   ├── gemini.py               # Google Gemini client integration
│   │   └── openai.py               # OpenAI client integration
│   ├── pipelines/                  # GenIA pipeline orchestration and stage implementations
│   │   ├── __init__.py             # Package marker
│   │   ├── genia_orchestrator.py   # Main orchestrator that coordinates the full pipeline
│   │   ├── shared.py               # Shared pipeline helpers reused by multiple stages
│   │   └── stages/                 # Individual pipeline stages
│   │       ├── __init__.py         # Package marker
│   │       ├── base.py             # Base stage abstractions
│   │       ├── confirmation.py     # Validation confirmation stage
│   │       ├── execution.py        # Test execution stage
│   │       ├── extraction.py       # UI / element extraction stage
│   │       ├── finalization.py     # Finalization stage
│   │       ├── generation.py       # Script generation stage
│   │       ├── homologation.py     # Homologation/reporting stage
│   │       ├── refactoring.py      # Script refactoring stage
│   │       ├── refinement.py       # Refinement stage
│   │       ├── structuring.py      # Test case restructuring stage
│   │       └── validation.py       # Human validation stage
│   ├── prompts/                    # Prompt templates used by the generation pipeline
│       ├── confirmation.txt        # Prompt for confirmation stage
│       ├── extraction.txt          # Prompt for extraction stage
│       ├── execution.txt           # Prompt for execution guidance
│       ├── finalization.txt        # Prompt for finalization stage
│       ├── homologation.txt        # Prompt for homologation stage
│       ├── refinement.txt          # Prompt for refinement stage
│       ├── refactoring.txt         # Prompt for refactoring stage
│       ├── structuring.txt         # Prompt for structuring stage
│       ├── structuring_user_history.txt  # Prompt variant for user-history based structuring
│       ├── validation.txt          # Prompt for validation stage
│       └── generation/             # Framework-specific generation prompts
│           ├── cypress.txt         # Cypress generation prompt
│           ├── junit.txt           # JUnit/Java generation prompt
│           ├── playwright.txt      # Playwright generation prompt
│           ├── pytest.txt          # Pytest generation prompt
│           ├── robot_framework.txt # Robot Framework generation prompt
│           └── selenium.txt        # Selenium generation prompt
├── frontend/                       # Browser UI for authentication, configuration, generation, and results
│   ├── index.html                  # Main single-page frontend entry
│   ├── styles.css                  # Global visual styling
│   ├── geniaLogo.jpg               # Branding asset used in the UI
│   └── scripts/                    # Modular frontend JavaScript code
│       ├── core/                   # Shared DOM, state, utility, and constant helpers
│       │   ├── constants.js        # Shared constant values and lookup tables
│       │   ├── dom.js              # DOM query helpers
│       │   ├── state.js            # In-memory app state and controller registry
│       │   └── utils.js            # Reusable helper utilities
│       ├── features/               # Screen-specific UI controllers and workflows
│       │   ├── auth.js             # Login and registration UI controller
│       │   ├── dashboard.js        # Dashboard metrics and summary controller
│       │   ├── generator.js        # Test generation workflow controller
│       │   ├── history.js          # Execution history UI controller
│       │   ├── llm-config.js       # LLM configuration management controller
│       │   └── navigation.js       # Sidebar navigation controller
│       ├── services/               # API, auth, and generation service layers
│       │   ├── api.js              # Backend API client
│       │   ├── auth.js             # Local authentication and persistence service
│       │   └── generation.js       # Generation pipeline service helpers
│       ├── ui/                     # Reusable UI layout and toast helpers
│       │   ├── layout.js           # Shell visibility and layout helpers
│       │   └── toast.js            # Toast notification helper
│       └── main.js                 # Frontend bootstrap and controller wiring
├── LICENSE                         # Open-source license for artifact distribution
└── README.md                       # Artifact documentation and usage guide
```

---

## 📋 Requirements

### Hardware

The artifact was evaluated using the following configuration:

- Operating System: Windows 11 Home Single Language 64-bit (Build 22631)
- Processor: Intel(R) Core(TM) i7-1165G7 @ 2.80GHz (8 cores)
- RAM: 12 GB
- Disk Space: At least 1 GB of free space

This hardware profile is sufficient for the local backend, the browser-based frontend, and the supported test execution strategies.

### Software

The artifact depends on:

- Python 3.12.3 or newer
- Google Chrome v135 or newer
- Git
- The Python dependencies listed in [`backend/requirements.txt`](backend/requirements.txt)

For the supported execution strategies, the environment must also allow the selected framework runtime to be launched locally. The backend is compatible with local execution on Windows, and the provided Dockerfile can be used when a containerized environment is preferred.

---

## ⚙️ Installation & Usage

1. Clone the repository:

```bash
git clone https://github.com/uffsoftwaretesting/GenIA-E2ETest-Platform.git
cd GenIA-E2ETest-Platform
```

2. Create and activate a Python virtual environment inside the backend folder:

```bash
cd backend
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
```

3. Install the backend dependencies:

```bash
pip install -r requirements.txt
playwright install
```

4. Start the backend API:

```bash
python api.py
```

By default, the backend starts on port `5000`.

5. In a second terminal, serve the frontend on a local HTTP origin:

```bash
cd ..\frontend
python -m http.server 5500
```

6. Open the frontend in the browser:

```text
http://localhost:5500
```

## Basic Usage

The following walkthrough describes a realistic end-to-end scenario using the platform.

### 1. Create an account

Register a new user by providing a username and password.

> Important: user data is stored only in memory during the current session. Once the browser session ends, no information is persisted.

### 2. Configure the AI model

Click on `Config. LLM` and fill in the form with:

- Configuration name: `LLM for GenIA testing`
- Provider: `OpenAI`
- Model: `gpt-5.6-terra`
- Temperature: `0`
- API Key: `[PASTE_YOUR_API_KEY]`

Then click `Save`.

### 3. Start the test generation process

Click `Generate Test`.

### 4. Provide the test case

In the `INPUT TYPE` section, select:

- `Free Input`
- `Test Case`

In the `Test Case` field, paste exactly the following content:

```text
urls: [https://demowebshop.tricentis.com, https://demowebshop.tricentis.com/login]

Test Case 3: Login User with incorrect email and password

1. Launch browser
2. Navigate to url 'https://demowebshop.tricentis.com'
3. Click on 'Log in' button
4. Enter an invalid email address "invalid_email_genia@gmail.com" and an invalid password "invalid_password"
5. Click 'Log in' button
6. Verify errors 'Login was unsuccessful. Please correct the errors and try again.' and 'No customer account found' is visible
```

### 5. Configure the project

In the `PROJECT` section, fill in:

- New project: `Demowebshop`
- Select project: `Demowebshop`
- Test name: `Login User with incorrect email and password`

### 6. Configure framework and language

In the `FRAMEWORK/LANGUAGE` section, select:

- Framework: `Robot Framework`
- Language: `Python`

### 7. Run the pipeline

Click `Generate Test`.

A bottom panel will appear showing the progress of the pipeline.

Follow the execution until the `Validation` stage.

### 8. Validation stage

When the pipeline reaches the `Validation` stage, click it.

Two buttons will be displayed.

Select:

- `Continue without changes`

### 9. End of the pipeline

After the `Validation` stage, the pipeline will continue automatically.

When the execution ends at the `Refactoring` stage:

- `Click Refactoring`
- `Click Finish execution`

---

## 📞 Support and Contact

For questions or support, please contact:
- Elvis Júnior 
- Vânia Neves 

---

## 📚 Citation
[![Cite this paper](https://img.shields.io/badge/Cite%20this%20paper-SBES%202026-blue)](#citation)

If you use **GenIA-E2ETest Platform** in your research or project, please cite:

```bibtex

@inproceedings{junior2026genia,
  author       = {Elvis Junior and Pedro Amaro and Allber Ferreira and Vânia O. Neves},
  title        = {GenIA‑E2ETest: A Generative AI‑Based Platform for End‑to‑End Test Automation},
  booktitle    = {Anais do XL Simpósio Brasileiro de Engenharia de Software},
  year         = {2026},
  address      = {São Paulo, SP, Brazil}
} 
