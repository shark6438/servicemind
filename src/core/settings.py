from enum import StrEnum
from json import loads
from typing import Annotated, Any

from dotenv import find_dotenv
from pydantic import (
    BeforeValidator,
    Field,
    HttpUrl,
    SecretStr,
    TypeAdapter,
    computed_field,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from schema.models import (
    AllModelEnum,
    AnthropicModelName,
    AWSModelName,
    AzureOpenAIModelName,
    DeepseekModelName,
    FakeModelName,
    GoogleModelName,
    GroqModelName,
    OllamaModelName,
    OpenAICompatibleName,
    OpenAIModelName,
    OpenRouterModelName,
    Provider,
    VertexAIModelName,
)


class DatabaseType(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"
    MONGO = "mongo"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    def to_logging_level(self) -> int:
        """Convert to Python logging level constant."""
        import logging

        mapping = {
            LogLevel.DEBUG: logging.DEBUG,
            LogLevel.INFO: logging.INFO,
            LogLevel.WARNING: logging.WARNING,
            LogLevel.ERROR: logging.ERROR,
            LogLevel.CRITICAL: logging.CRITICAL,
        }
        return mapping[self]


def check_str_is_http(x: str) -> str:
    http_url_adapter = TypeAdapter(HttpUrl)
    return str(http_url_adapter.validate_python(x))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=find_dotenv(),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        validate_default=False,
    )
    MODE: str | None = None

    HOST: str = "0.0.0.0"
    PORT: int = 8080
    GRACEFUL_SHUTDOWN_TIMEOUT: int = 30
    LOG_LEVEL: LogLevel = LogLevel.WARNING

    AUTH_SECRET: SecretStr | None = None

    OPENAI_API_KEY: SecretStr | None = None
    DEEPSEEK_API_KEY: SecretStr | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    GOOGLE_APPLICATION_CREDENTIALS: SecretStr | None = None
    GROQ_API_KEY: SecretStr | None = None
    USE_AWS_BEDROCK: bool = False
    OLLAMA_MODEL: str | None = None
    OLLAMA_BASE_URL: str | None = None
    USE_FAKE_MODEL: bool = False
    OPENROUTER_API_KEY: SecretStr | None = None

    # If DEFAULT_MODEL is None, it will be set in model_post_init
    DEFAULT_MODEL: AllModelEnum | None = None  # type: ignore[assignment]
    AVAILABLE_MODELS: set[AllModelEnum] = set()  # type: ignore[assignment]

    # Set openai compatible api, mainly used for proof of concept
    COMPATIBLE_MODEL: str | None = None
    COMPATIBLE_API_KEY: SecretStr | None = None
    COMPATIBLE_BASE_URL: str | None = None

    OPENWEATHERMAP_API_KEY: SecretStr | None = None

    # MCP Configuration
    GITHUB_PAT: SecretStr | None = None
    MCP_GITHUB_SERVER_URL: str = "https://api.githubcopilot.com/mcp/"

    # ServiceMind GLPI integration
    GLPI_BASE_URL: str | None = None
    GLPI_API_VERSION: str = "v2.3"
    GLPI_CLIENT_ID: SecretStr | None = None
    GLPI_CLIENT_SECRET: SecretStr | None = None
    GLPI_USERNAME: str | None = None
    GLPI_PASSWORD: SecretStr | None = None
    GLPI_ENTITY_ID: int = 0
    GLPI_PROFILE_ID: int = 6
    GLPI_TIMEOUT_SECONDS: float = 15.0
    GLPI_TRUST_ENV: bool = False

    # ServiceMind Phase 2 enterprise runtime
    SERVICEMIND_DATABASE_URL: SecretStr | None = None
    SERVICEMIND_MIGRATION_DATABASE_URL: SecretStr | None = None
    SERVICEMIND_CREDENTIAL_KEY: SecretStr | None = None
    SERVICEMIND_OIDC_ISSUER: str | None = None
    SERVICEMIND_OIDC_AUDIENCE: str = "servicemind-api"
    SERVICEMIND_OIDC_JWKS_URL: str | None = None
    SERVICEMIND_ACME_GLPI_USERNAME: str | None = None
    SERVICEMIND_ACME_GLPI_PASSWORD: SecretStr | None = None
    SERVICEMIND_GLOBEX_GLPI_USERNAME: str | None = None
    SERVICEMIND_GLOBEX_GLPI_PASSWORD: SecretStr | None = None
    SERVICEMIND_ACME_WEBHOOK_SECRET: SecretStr | None = None
    SERVICEMIND_GLOBEX_WEBHOOK_SECRET: SecretStr | None = None
    SERVICEMIND_OTEL_ENDPOINT: str | None = None
    SERVICEMIND_OTEL_SERVICE_NAME: str = "servicemind-api"
    SERVICEMIND_MAX_TASKS: int = 12
    SERVICEMIND_MAX_PARALLEL: int = 4
    SERVICEMIND_MAX_REPLANS: int = 2
    SERVICEMIND_MAX_STEPS: int = 64
    SERVICEMIND_MAX_MODEL_CALLS: int = 32
    SERVICEMIND_MAX_TOOL_CALLS: int = 32
    SERVICEMIND_RUN_DEADLINE_SECONDS: int = 600
    SERVICEMIND_RAG_ENABLED: bool = False
    SERVICEMIND_RAG_REQUIRED: bool = False
    SERVICEMIND_OPENSEARCH_URL: str = "https://127.0.0.1:9200"
    SERVICEMIND_OPENSEARCH_USERNAME: str = "admin"
    SERVICEMIND_OPENSEARCH_PASSWORD: SecretStr | None = None
    SERVICEMIND_OPENSEARCH_VERIFY_CERTS: bool = False
    SERVICEMIND_EMBEDDING_MODEL: str = "BAAI/bge-m3"
    #: MUST be pinned to a concrete commit hash in production. Floating "main" silently
    #: changes the vector space; previously indexed chunks lose Recall.
    SERVICEMIND_EMBEDDING_REVISION: str = "main"
    SERVICEMIND_RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"
    #: MUST be pinned alongside SERVICEMIND_EMBEDDING_REVISION (see comment above).
    SERVICEMIND_RERANKER_REVISION: str = "main"
    SERVICEMIND_MODEL_DEVICE: str | None = None
    #: huggingface_hub cache roots for the in-process fallback providers (the repo's
    #: pre-fetched snapshots live under ``data/phase4/models/{embedding,reranker}``).
    #: When both cache dir and revision are pinned, in-process load is fully offline.
    SERVICEMIND_EMBEDDING_CACHE_DIR: str | None = None
    SERVICEMIND_RERANKER_CACHE_DIR: str | None = None
    #: Graph-RAG (Phase 4 baseline 4.2): optional structural retrieval over the
    #: Neo4j projection (Ticket/Ci/Service/Problem/Change). Text hybrid retrieval
    #: remains the primary channel; graph findings are a side channel.
    SERVICEMIND_GRAPH_RAG_ENABLED: bool = False
    NEO4J_URI: str = "bolt://127.0.0.1:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: SecretStr | None = None
    SERVICEMIND_RAG_CONTEXT_TOKENS: int = 8000
    # Context packer diversity ceilings (Phase 4 baseline §7). A single document
    # must not crowd the context, and no single source may drown every other source;
    # selection still only packs parents whose rerank earned them a place.
    SERVICEMIND_RAG_MAX_PARENTS_PER_DOCUMENT: int = 2
    SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE: int = 4
    #: Multi-query fan-out (hybrid only): add one BM25 sub-query per LLM rewrite to
    #: the unchanged single dense anchor, inside one OpenSearch ``hybrid`` query that
    #: the cluster RRF merges (OpenSearch caps ``hybrid`` at 5 arms, so rewrites
    #: expand lexical coverage rather than duplicating dense vectors). The rewrite
    #: stage always runs; disable to revert to today's single-query two-arm request
    #: when the extra lexical arms' latency is unwanted.
    SERVICEMIND_RAG_MULTI_QUERY: bool = True
    #: Candidate funnel for hybrid retrieval (RRF). The dense and BM25 arms each
    #: fetch ``_ARM_K`` hits and RRF fuses them down to ``_CANDIDATE_K`` rows that
    #: reach the cross-encoder rerank. Keeping the arms wider than the fused pool
    #: gives the reranker material to promote items neither channel ranked alone;
    #: ``candidate_k`` is therefore the effective recall ceiling and costs one
    #: rerank score per row. Operators raise the funnel for large corpora, lower it
    #: to bound rerank latency.
    SERVICEMIND_RAG_DENSE_K: int = Field(default=60, ge=1)
    SERVICEMIND_RAG_BM25_K: int = Field(default=60, ge=1)
    SERVICEMIND_RAG_CANDIDATE_K: int = Field(default=40, ge=1)
    SERVICEMIND_EMBEDDING_URL: str | None = None
    SERVICEMIND_RERANKER_URL: str | None = None

    # ServiceMind Phase 5 governance. Long-term memory and context injection are
    # feature-gated independently so rollout and rollback never require a schema
    # downgrade. The Model Gateway path itself is always used; this flag controls
    # durable per-call audit persistence.
    SERVICEMIND_MEMORY_ENABLED: bool = False
    SERVICEMIND_MEMORY_AUTO_ACTIVATION_CONFIDENCE: float = 0.9
    SERVICEMIND_MEMORY_VECTOR_ENABLED: bool = False
    SERVICEMIND_MEMORY_CANDIDATE_CEILING: int = Field(default=100, ge=1, le=500)
    SERVICEMIND_CONTEXT_ENABLED: bool = False
    SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS: int = 12_000
    SERVICEMIND_SKILLS_ENABLED: bool = False
    SERVICEMIND_SKILLS_DIR: str = "skills"
    SERVICEMIND_MODEL_GATEWAY_AUDIT_ENABLED: bool = False
    SERVICEMIND_MODEL_ALLOWED_PROVIDERS: str = (
        "deepseek,openai,azure,anthropic,google,vertexai,groq,aws,ollama,openrouter,fake"
    )
    SERVICEMIND_MODEL_ALLOWED_MODELS: str = "*"
    SERVICEMIND_TENANT_MODEL_ALLOWLIST_JSON: str = "{}"
    SERVICEMIND_MODEL_MAX_RETRIES: int = 1
    SERVICEMIND_MODEL_TIMEOUT_SECONDS: float = 30
    SERVICEMIND_MODEL_MAX_COST_USD_PER_CALL: float = Field(default=0.05, gt=0, le=10)
    SERVICEMIND_SEMANTIC_CACHE_ENABLED: bool = False

    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_PROJECT: str = "default"
    LANGCHAIN_ENDPOINT: Annotated[str, BeforeValidator(check_str_is_http)] = (
        "https://api.smith.langchain.com"
    )
    LANGCHAIN_API_KEY: SecretStr | None = None

    LANGFUSE_TRACING: bool = False
    LANGFUSE_HOST: Annotated[str, BeforeValidator(check_str_is_http)] = "https://cloud.langfuse.com"
    LANGFUSE_PUBLIC_KEY: SecretStr | None = None
    LANGFUSE_SECRET_KEY: SecretStr | None = None

    # Database Configuration
    DATABASE_TYPE: DatabaseType = (
        DatabaseType.SQLITE
    )  # Options: DatabaseType.SQLITE or DatabaseType.POSTGRES
    SQLITE_DB_PATH: str = "checkpoints.db"

    # PostgreSQL Configuration
    POSTGRES_USER: str | None = None
    POSTGRES_PASSWORD: SecretStr | None = None
    POSTGRES_HOST: str | None = None
    POSTGRES_PORT: int | None = None
    POSTGRES_DB: str | None = None
    POSTGRES_APPLICATION_NAME: str = "servicemind"
    POSTGRES_MIN_CONNECTIONS_PER_POOL: int = 1
    POSTGRES_MAX_CONNECTIONS_PER_POOL: int = 1
    POSTGRES_AUTO_SETUP: bool = True

    # MongoDB Configuration
    MONGO_HOST: str | None = None
    MONGO_PORT: int | None = None
    MONGO_DB: str | None = None
    MONGO_USER: str | None = None
    MONGO_PASSWORD: SecretStr | None = None
    MONGO_AUTH_SOURCE: str | None = None
    MONGO_TLS: bool = False  # opt-in TLS for MongoDB; set to True for production/Atlas

    # Azure OpenAI Settings
    AZURE_OPENAI_API_KEY: SecretStr | None = None
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_VERSION: str = "2024-02-15-preview"
    AZURE_OPENAI_DEPLOYMENT_MAP: dict[str, str] = Field(
        default_factory=dict, description="Map of model names to Azure deployment IDs"
    )

    def model_post_init(self, __context: Any) -> None:
        api_keys = {
            Provider.OPENAI: self.OPENAI_API_KEY,
            Provider.OPENAI_COMPATIBLE: self.COMPATIBLE_BASE_URL and self.COMPATIBLE_MODEL,
            Provider.DEEPSEEK: self.DEEPSEEK_API_KEY,
            Provider.ANTHROPIC: self.ANTHROPIC_API_KEY,
            Provider.GOOGLE: self.GOOGLE_API_KEY,
            Provider.VERTEXAI: self.GOOGLE_APPLICATION_CREDENTIALS,
            Provider.GROQ: self.GROQ_API_KEY,
            Provider.AWS: self.USE_AWS_BEDROCK,
            Provider.OLLAMA: self.OLLAMA_MODEL,
            Provider.FAKE: self.USE_FAKE_MODEL,
            Provider.AZURE_OPENAI: self.AZURE_OPENAI_API_KEY,
            Provider.OPENROUTER: self.OPENROUTER_API_KEY,
        }
        active_keys = [k for k, v in api_keys.items() if v]
        if not active_keys:
            raise ValueError("At least one LLM API key must be provided.")

        # USE_FAKE_MODEL must win the default even when real provider keys are present.
        if self.USE_FAKE_MODEL and self.DEFAULT_MODEL is None:
            self.DEFAULT_MODEL = FakeModelName.FAKE

        for provider in active_keys:
            match provider:
                case Provider.OPENAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenAIModelName.GPT_5_NANO
                    self.AVAILABLE_MODELS.update(set(OpenAIModelName))
                case Provider.OPENAI_COMPATIBLE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenAICompatibleName.OPENAI_COMPATIBLE
                    self.AVAILABLE_MODELS.update(set(OpenAICompatibleName))
                case Provider.DEEPSEEK:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = DeepseekModelName.DEEPSEEK_V4_FLASH
                    self.AVAILABLE_MODELS.update(set(DeepseekModelName))
                case Provider.ANTHROPIC:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AnthropicModelName.HAIKU_45
                    self.AVAILABLE_MODELS.update(set(AnthropicModelName))
                case Provider.GOOGLE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = GoogleModelName.GEMINI_36_FLASH
                    self.AVAILABLE_MODELS.update(set(GoogleModelName))
                case Provider.VERTEXAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = VertexAIModelName.GEMINI_36_FLASH
                    self.AVAILABLE_MODELS.update(set(VertexAIModelName))
                case Provider.GROQ:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = GroqModelName.GPT_OSS_20B
                    self.AVAILABLE_MODELS.update(set(GroqModelName))
                case Provider.AWS:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AWSModelName.BEDROCK_HAIKU
                    self.AVAILABLE_MODELS.update(set(AWSModelName))
                case Provider.OLLAMA:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OllamaModelName.OLLAMA_GENERIC
                    self.AVAILABLE_MODELS.update(set(OllamaModelName))
                case Provider.OPENROUTER:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenRouterModelName.GEMINI_36_FLASH
                    self.AVAILABLE_MODELS.update(set(OpenRouterModelName))
                case Provider.FAKE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = FakeModelName.FAKE
                    self.AVAILABLE_MODELS.update(set(FakeModelName))
                case Provider.AZURE_OPENAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AzureOpenAIModelName.AZURE_GPT_5_MINI
                    self.AVAILABLE_MODELS.update(set(AzureOpenAIModelName))
                    # Validate Azure OpenAI settings if Azure provider is available
                    if not self.AZURE_OPENAI_API_KEY:
                        raise ValueError("AZURE_OPENAI_API_KEY must be set")
                    if not self.AZURE_OPENAI_ENDPOINT:
                        raise ValueError("AZURE_OPENAI_ENDPOINT must be set")
                    if not self.AZURE_OPENAI_DEPLOYMENT_MAP:
                        raise ValueError("AZURE_OPENAI_DEPLOYMENT_MAP must be set")

                    # Parse deployment map if it's a string
                    if isinstance(self.AZURE_OPENAI_DEPLOYMENT_MAP, str):
                        try:
                            self.AZURE_OPENAI_DEPLOYMENT_MAP = loads(
                                self.AZURE_OPENAI_DEPLOYMENT_MAP
                            )
                        except Exception as e:
                            raise ValueError(f"Invalid AZURE_OPENAI_DEPLOYMENT_MAP JSON: {e}")

                    # Validate required deployments exist
                    required_models = {"gpt-5", "gpt-5-mini"}
                    missing_models = required_models - set(self.AZURE_OPENAI_DEPLOYMENT_MAP.keys())
                    if missing_models:
                        raise ValueError(f"Missing required Azure deployments: {missing_models}")
                case _:
                    raise ValueError(f"Unknown provider: {provider}")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def BASE_URL(self) -> str:
        return f"http://{self.HOST}:{self.PORT}"

    def is_dev(self) -> bool:
        return self.MODE == "dev"


settings = Settings()
