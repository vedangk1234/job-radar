import { compileTerm } from "./match";

/**
 * The extraction vocabulary: what a resume is scanned AGAINST, and the scan
 * itself. Deterministic, offline, no model and no tokens.
 *
 * WHAT THIS IS FOR, AND WHAT IT IS NOT. Uploading a resume produces a list of
 * SUGGESTIONS which a person then edits — removing what does not belong, adding
 * what this list has never heard of — and the edited result is saved to
 * preferences.keywords. So this vocabulary decides what gets OFFERED, never what
 * gets stored and never what matches. Being wrong here costs a chip somebody
 * deletes; the closed vocabulary that actually decides something is
 * lib/labels.CITY_LABELS, and it is closed for the opposite reason.
 *
 * IT IS DELIBERATELY MUCH BROADER THAN DEFAULT_PREFERENCES.keywords. That list
 * is 43 terms describing the roles ONE person wants, and matching it against a
 * resume would suggest backend keywords to a data scientist and nothing at all
 * to an Android developer. This one is written to recognise any engineer:
 * languages, web and mobile frameworks, data, ML, cloud, infrastructure,
 * databases, testing and the tools around them.
 *
 * WHY IT IS DATA IN THIS REPO RATHER THAN A DEPENDENCY. There is no maintained
 * npm package for this that is not either a scraped dump with an unclear licence
 * or a wrapper around somebody's API. Written here it is reviewable in a diff,
 * costs no build step, resolves no network at request time, and answers the same
 * way in a year. The sources it was compiled from are public taxonomies: GitHub
 * Linguist's language list, the Stack Overflow Developer Survey's technology
 * sections, the CNCF landscape, and the standard data/ML stack.
 *
 * NOTHING HERE IS A CONTENT FILTER. The vocabulary decides what can be
 * SUGGESTED; a person may type anything at all, and the editor neither
 * autocompletes from this list nor rejects a term that is absent from it. See
 * components/KeywordChips.tsx.
 */

/**
 * One entry. `term` is the canonical form, lowercase — because
 * preferences.keywords is lowercase, because notify/match.py compares against a
 * normalised title. `aliases` are other spellings that mean the same thing and
 * collapse onto `term`, so a resume saying "postgres" and one saying
 * "PostgreSQL" suggest the same chip.
 *
 * `strict` is the escape hatch for terms whose lowercase form is an ordinary
 * English word. See STRICT below — it is the difference between suggesting `go`
 * to a Go developer and suggesting it to everybody who wrote "go-to-market".
 */
export type Skill = {
  readonly term: string;
  readonly aliases?: readonly string[];
  readonly strict?: readonly string[];
};

/**
 * THE AMBIGUOUS ONES, and why they need their own rule.
 *
 * `\bgo\b` matches "go live", "go-to-market" and "ready to go", which is to say
 * it matches almost every resume ever written. The same is true of `express`,
 * `spring`, `swift`, `dart`, `rust`, `scala`, `elm`, `next` and `solid`. Word
 * boundaries are the right rule for a job TITLE, where the vocabulary is small
 * and the text is eight words long; over two pages of prose they are not enough.
 *
 * So these carry `strict`: the exact CASE-SENSITIVE spellings that count,
 * matched against the original text rather than the lowercased copy. A person
 * who lists "Go" or "Golang" gets the chip; a person who "went the extra mile to
 * go beyond" does not. The chip itself is still stored lowercase, because that
 * is what the matcher needs.
 *
 * Single letters (`c`, `r`, `d`) need a second rule on top, because even
 * case-sensitively "R" appears in initials, "C" in "B.C." and both in headings.
 * They must also sit in a DELIMITED RUN — next to a comma, slash, semicolon or
 * pipe — which is how a skills list is actually written ("Languages: C, C++,
 * Go") and is not how a name is. See matchesSingleLetter.
 */

// prettier-ignore
export const SKILL_VOCABULARY: readonly Skill[] = [
  // --- languages ----------------------------------------------------------
  { term: "python" },
  { term: "javascript", aliases: ["js", "ecmascript"] },
  { term: "typescript", aliases: ["ts"] },
  { term: "java" },
  { term: "kotlin" },
  { term: "c++", aliases: ["cpp", "cplusplus"] },
  { term: "c#", aliases: ["csharp", "c sharp"] },
  { term: "c", strict: ["C"] },
  { term: "go", strict: ["Go", "Golang", "GoLang"] },
  { term: "rust", strict: ["Rust"] },
  { term: "ruby" },
  { term: "php" },
  { term: "swift", strict: ["Swift"] },
  { term: "objective-c", aliases: ["objectivec", "obj-c"] },
  { term: "scala", strict: ["Scala"] },
  { term: "perl" },
  { term: "haskell" },
  { term: "elixir" },
  { term: "erlang" },
  { term: "clojure" },
  { term: "lua" },
  { term: "dart", strict: ["Dart"] },
  { term: "r", strict: ["R"] },
  { term: "matlab" },
  { term: "julia", strict: ["Julia"] },
  { term: "groovy" },
  { term: "f#", aliases: ["fsharp"] },
  { term: "ocaml" },
  { term: "fortran" },
  { term: "cobol" },
  { term: "assembly" },
  { term: "solidity" },
  { term: "zig" },
  { term: "nim", strict: ["Nim"] },
  { term: "crystal", strict: ["Crystal"] },
  { term: "visual basic", aliases: ["vb.net", "vba"] },
  { term: "delphi" },
  { term: "abap" },
  { term: "apex" },
  { term: "shell", aliases: ["bash", "zsh", "shell scripting", "sh"] },
  { term: "powershell" },
  { term: "sql" },
  { term: "plsql", aliases: ["pl/sql"] },
  { term: "t-sql", aliases: ["tsql", "transact-sql"] },
  { term: "html", aliases: ["html5"] },
  { term: "css", aliases: ["css3"] },
  { term: "sass", aliases: ["scss"] },
  { term: "less" },
  { term: "graphql" },
  { term: "wasm", aliases: ["webassembly"] },

  // --- frontend -----------------------------------------------------------
  { term: "react", aliases: ["react.js", "reactjs"] },
  { term: "next.js", aliases: ["nextjs"] },
  { term: "vue", aliases: ["vue.js", "vuejs"] },
  { term: "nuxt", aliases: ["nuxt.js", "nuxtjs"] },
  { term: "angular", aliases: ["angularjs", "angular.js"] },
  { term: "svelte", aliases: ["sveltekit"] },
  { term: "solid.js", aliases: ["solidjs"] },
  { term: "ember", aliases: ["ember.js", "emberjs"] },
  { term: "backbone", aliases: ["backbone.js"] },
  { term: "jquery" },
  { term: "redux" },
  { term: "mobx" },
  { term: "zustand" },
  { term: "recoil" },
  { term: "rxjs" },
  { term: "tailwind", aliases: ["tailwindcss", "tailwind css"] },
  { term: "bootstrap" },
  { term: "material ui", aliases: ["mui", "material-ui"] },
  { term: "chakra ui", aliases: ["chakra"] },
  { term: "styled components", aliases: ["styled-components"] },
  { term: "webpack" },
  { term: "vite" },
  { term: "rollup" },
  { term: "esbuild" },
  { term: "babel" },
  { term: "turbopack" },
  { term: "storybook" },
  { term: "three.js", aliases: ["threejs"] },
  { term: "d3", aliases: ["d3.js", "d3js"] },
  { term: "webgl" },
  { term: "web components" },
  { term: "responsive design" },
  { term: "accessibility", aliases: ["a11y", "wcag"] },
  { term: "astro", strict: ["Astro"] },
  { term: "remix", strict: ["Remix"] },
  { term: "gatsby" },
  { term: "electron" },

  // --- backend / web frameworks -------------------------------------------
  { term: "node", aliases: ["node.js", "nodejs"] },
  { term: "express", strict: ["Express", "Express.js", "ExpressJS", "expressjs"] },
  { term: "nestjs", aliases: ["nest.js"] },
  { term: "fastify" },
  { term: "koa" },
  { term: "deno" },
  { term: "bun", strict: ["Bun"] },
  { term: "django" },
  { term: "flask" },
  { term: "fastapi" },
  { term: "pyramid", strict: ["Pyramid"] },
  { term: "tornado", strict: ["Tornado"] },
  { term: "celery" },
  { term: "spring", strict: ["Spring"] },
  { term: "spring boot", aliases: ["springboot"] },
  { term: "hibernate" },
  { term: "micronaut" },
  { term: "quarkus" },
  { term: "vert.x", aliases: ["vertx"] },
  { term: "struts" },
  { term: "jsp" },
  { term: "jpa" },
  { term: "servlet" },
  { term: "rails", aliases: ["ruby on rails", "ror"] },
  { term: "sinatra" },
  { term: "laravel" },
  { term: "symfony" },
  { term: "codeigniter" },
  { term: "asp.net", aliases: ["aspnet", "asp.net core"] },
  { term: ".net", aliases: ["dotnet", ".net core"] },
  { term: "entity framework" },
  { term: "gin", strict: ["Gin"] },
  { term: "echo", strict: ["Echo"] },
  { term: "fiber", strict: ["Fiber"] },
  { term: "actix" },
  { term: "axum", strict: ["Axum"] },
  { term: "rocket", strict: ["Rocket"] },
  { term: "phoenix", strict: ["Phoenix"] },
  { term: "api", aliases: ["apis"] },
  { term: "rest", aliases: ["rest api", "restful"] },
  { term: "grpc" },
  { term: "soap" },
  { term: "websockets", aliases: ["websocket"] },
  { term: "webrtc" },
  { term: "openapi", aliases: ["swagger"] },
  { term: "protobuf", aliases: ["protocol buffers"] },
  { term: "microservices", aliases: ["microservice"] },
  { term: "monolith" },
  { term: "serverless" },
  { term: "event driven", aliases: ["event-driven"] },
  { term: "message queue", aliases: ["message queues"] },
  { term: "distributed systems" },
  { term: "system design" },
  { term: "api gateway" },
  { term: "oauth", aliases: ["oauth2", "oauth 2.0"] },
  { term: "jwt" },
  { term: "saml" },
  { term: "openid connect", aliases: ["oidc"] },
  { term: "single sign on", aliases: ["sso"] },

  // --- mobile -------------------------------------------------------------
  { term: "android" },
  { term: "ios" },
  { term: "react native" },
  { term: "flutter" },
  { term: "swiftui" },
  { term: "uikit" },
  { term: "jetpack compose" },
  { term: "xamarin" },
  { term: "ionic" },
  { term: "cordova" },
  { term: "capacitor" },
  { term: "kotlin multiplatform" },

  // --- databases ----------------------------------------------------------
  { term: "postgresql", aliases: ["postgres", "psql"] },
  { term: "mysql" },
  { term: "mariadb" },
  { term: "sqlite" },
  { term: "oracle" },
  { term: "sql server", aliases: ["mssql", "microsoft sql server"] },
  { term: "mongodb", aliases: ["mongo"] },
  { term: "cassandra" },
  { term: "dynamodb" },
  { term: "couchbase" },
  { term: "couchdb" },
  { term: "redis" },
  { term: "memcached" },
  { term: "elasticsearch", aliases: ["elastic search", "opensearch"] },
  { term: "solr" },
  { term: "neo4j" },
  { term: "influxdb" },
  { term: "timescaledb" },
  { term: "clickhouse" },
  { term: "cockroachdb" },
  { term: "firestore" },
  { term: "firebase" },
  { term: "supabase" },
  { term: "planetscale" },
  { term: "duckdb" },
  { term: "snowflake" },
  { term: "bigquery" },
  { term: "redshift" },
  { term: "synapse" },
  { term: "teradata" },
  { term: "hbase" },
  { term: "hive" },
  { term: "presto", aliases: ["trino"] },
  { term: "database design" },
  { term: "query optimization", aliases: ["query optimisation"] },
  { term: "indexing" },
  { term: "sharding" },
  { term: "replication" },
  { term: "orm" },
  { term: "prisma" },
  { term: "sqlalchemy" },
  { term: "sequelize" },
  { term: "typeorm" },
  { term: "drizzle" },
  { term: "knex" },
  { term: "liquibase" },
  { term: "flyway" },

  // --- cloud --------------------------------------------------------------
  { term: "aws", aliases: ["amazon web services"] },
  { term: "azure", aliases: ["microsoft azure"] },
  { term: "gcp", aliases: ["google cloud", "google cloud platform"] },
  { term: "cloud", aliases: ["cloud computing"] },
  { term: "ec2" },
  { term: "s3" },
  { term: "lambda", strict: ["Lambda"] },
  { term: "ecs" },
  { term: "eks" },
  { term: "fargate" },
  { term: "cloudfront" },
  { term: "route53", aliases: ["route 53"] },
  { term: "cloudformation" },
  { term: "cloudwatch" },
  { term: "sqs" },
  { term: "sns" },
  { term: "step functions" },
  { term: "aurora", strict: ["Aurora"] },
  { term: "athena", strict: ["Athena"] },
  { term: "glue", strict: ["Glue"] },
  { term: "emr" },
  { term: "sagemaker" },
  { term: "app engine" },
  { term: "cloud run" },
  { term: "cloud functions" },
  { term: "pub/sub", aliases: ["pubsub"] },
  { term: "dataflow" },
  { term: "dataproc" },
  { term: "vertex ai" },
  { term: "aks" },
  { term: "azure functions" },
  { term: "azure devops" },
  { term: "digitalocean", aliases: ["digital ocean"] },
  { term: "heroku" },
  { term: "vercel" },
  { term: "netlify" },
  { term: "cloudflare" },
  { term: "railway", strict: ["Railway"] },
  { term: "fly.io" },
  { term: "render", strict: ["Render"] },

  // --- devops / infrastructure --------------------------------------------
  { term: "docker" },
  { term: "kubernetes", aliases: ["k8s"] },
  { term: "helm" },
  { term: "openshift" },
  { term: "rancher" },
  { term: "istio" },
  { term: "linkerd" },
  { term: "envoy" },
  { term: "consul" },
  { term: "vault", strict: ["Vault"] },
  { term: "nomad", strict: ["Nomad"] },
  { term: "terraform" },
  { term: "pulumi" },
  { term: "ansible" },
  { term: "puppet" },
  { term: "chef", strict: ["Chef"] },
  { term: "saltstack" },
  { term: "vagrant" },
  { term: "packer", strict: ["Packer"] },
  { term: "jenkins" },
  { term: "circleci", aliases: ["circle ci"] },
  { term: "travis ci", aliases: ["travisci"] },
  { term: "github actions" },
  { term: "gitlab ci", aliases: ["gitlab-ci"] },
  { term: "bamboo", strict: ["Bamboo"] },
  { term: "teamcity" },
  { term: "argocd", aliases: ["argo cd", "argo"] },
  { term: "spinnaker" },
  { term: "flux", strict: ["Flux"] },
  { term: "ci/cd", aliases: ["cicd", "ci cd", "continuous integration", "continuous delivery"] },
  { term: "devops" },
  { term: "sre", aliases: ["site reliability"] },
  { term: "platform engineering" },
  { term: "infrastructure as code", aliases: ["iac"] },
  { term: "gitops" },
  { term: "nginx" },
  { term: "apache" },
  { term: "haproxy" },
  { term: "traefik" },
  { term: "caddy", strict: ["Caddy"] },
  { term: "load balancing" },
  { term: "linux" },
  { term: "unix" },
  { term: "ubuntu" },
  { term: "debian" },
  { term: "centos" },
  { term: "rhel", aliases: ["red hat"] },
  { term: "windows server" },
  { term: "networking" },
  { term: "tcp/ip", aliases: ["tcp ip"] },
  { term: "dns" },
  { term: "vpn" },
  { term: "firewall" },
  { term: "prometheus" },
  { term: "grafana" },
  { term: "datadog" },
  { term: "new relic", aliases: ["newrelic"] },
  { term: "splunk" },
  { term: "sentry" },
  { term: "opentelemetry", aliases: ["otel"] },
  { term: "jaeger" },
  { term: "elk", aliases: ["elk stack"] },
  { term: "logstash" },
  { term: "kibana" },
  { term: "pagerduty" },
  { term: "observability" },
  { term: "monitoring" },
  { term: "incident response" },
  { term: "capacity planning" },
  { term: "performance tuning" },
  { term: "scalability" },
  { term: "high availability" },
  { term: "disaster recovery" },

  // --- data engineering ---------------------------------------------------
  { term: "hadoop" },
  { term: "spark", aliases: ["apache spark", "pyspark"] },
  { term: "flink" },
  { term: "beam", strict: ["Beam"] },
  { term: "storm", strict: ["Storm"] },
  { term: "kafka" },
  { term: "rabbitmq" },
  { term: "activemq" },
  { term: "pulsar" },
  { term: "nats" },
  { term: "airflow" },
  { term: "dagster" },
  { term: "prefect" },
  { term: "luigi" },
  { term: "dbt" },
  { term: "fivetran" },
  { term: "airbyte" },
  { term: "talend" },
  { term: "informatica" },
  { term: "ssis" },
  { term: "etl" },
  { term: "elt" },
  { term: "data pipeline", aliases: ["data pipelines"] },
  { term: "data warehouse", aliases: ["data warehousing"] },
  { term: "data lake" },
  { term: "data modeling", aliases: ["data modelling"] },
  { term: "data governance" },
  { term: "data quality" },
  { term: "databricks" },
  { term: "delta lake" },
  { term: "iceberg", strict: ["Iceberg"] },
  { term: "hudi" },
  { term: "parquet" },
  { term: "avro" },
  { term: "orc" },
  { term: "streaming" },
  { term: "batch processing" },
  { term: "mapreduce" },

  // --- data science / analytics -------------------------------------------
  { term: "pandas" },
  { term: "numpy" },
  { term: "scipy" },
  { term: "polars" },
  { term: "matplotlib" },
  { term: "seaborn" },
  { term: "plotly" },
  { term: "jupyter" },
  { term: "tableau" },
  { term: "power bi", aliases: ["powerbi"] },
  { term: "looker" },
  { term: "metabase" },
  { term: "superset" },
  { term: "qlik" },
  { term: "excel" },
  { term: "statistics" },
  { term: "data analysis", aliases: ["data analytics"] },
  { term: "data visualization", aliases: ["data visualisation"] },
  { term: "ab testing", aliases: ["a/b testing"] },
  { term: "experimentation" },
  { term: "forecasting" },
  { term: "time series" },

  // --- machine learning / AI ----------------------------------------------
  { term: "machine learning", aliases: ["ml"] },
  { term: "deep learning" },
  { term: "artificial intelligence", aliases: ["ai"] },
  { term: "neural networks", aliases: ["neural network"] },
  { term: "tensorflow" },
  { term: "pytorch" },
  { term: "keras" },
  { term: "scikit-learn", aliases: ["sklearn", "scikit learn"] },
  { term: "xgboost" },
  { term: "lightgbm" },
  { term: "catboost" },
  { term: "hugging face", aliases: ["huggingface", "transformers"] },
  { term: "langchain" },
  { term: "llamaindex", aliases: ["llama index"] },
  { term: "openai" },
  { term: "llm", aliases: ["llms", "large language model", "large language models"] },
  { term: "rag", aliases: ["retrieval augmented generation"] },
  { term: "prompt engineering" },
  { term: "fine tuning", aliases: ["fine-tuning"] },
  { term: "embeddings" },
  { term: "vector database", aliases: ["vector db"] },
  { term: "pinecone" },
  { term: "weaviate" },
  { term: "chroma", strict: ["Chroma", "ChromaDB"] },
  { term: "faiss" },
  { term: "milvus" },
  { term: "qdrant" },
  { term: "nlp", aliases: ["natural language processing"] },
  { term: "computer vision", aliases: ["cv"] },
  { term: "opencv" },
  { term: "yolo" },
  { term: "reinforcement learning" },
  { term: "recommendation systems", aliases: ["recommender systems", "recsys"] },
  { term: "mlops" },
  { term: "mlflow" },
  { term: "kubeflow" },
  { term: "weights and biases", aliases: ["wandb"] },
  { term: "feature engineering" },
  { term: "feature store" },
  { term: "model deployment" },
  { term: "model serving" },
  { term: "onnx" },
  { term: "cuda" },
  { term: "gpu" },
  { term: "spacy" },
  { term: "nltk" },
  { term: "gensim" },
  { term: "generative ai", aliases: ["genai"] },
  { term: "diffusion models" },
  { term: "transformers architecture" },

  // --- testing / quality --------------------------------------------------
  { term: "unit testing", aliases: ["unit tests"] },
  { term: "integration testing" },
  { term: "end to end testing", aliases: ["e2e testing"] },
  { term: "test automation" },
  { term: "tdd", aliases: ["test driven development"] },
  { term: "bdd" },
  { term: "jest" },
  { term: "vitest" },
  { term: "mocha" },
  { term: "chai" },
  { term: "jasmine" },
  { term: "cypress" },
  { term: "playwright" },
  { term: "selenium" },
  { term: "puppeteer" },
  { term: "testing library" },
  { term: "pytest" },
  { term: "unittest" },
  { term: "junit" },
  { term: "testng" },
  { term: "rspec" },
  { term: "cucumber" },
  { term: "postman" },
  { term: "jmeter" },
  { term: "locust", strict: ["Locust"] },
  { term: "k6" },
  { term: "load testing" },
  { term: "performance testing" },
  { term: "code review" },
  { term: "static analysis" },
  { term: "sonarqube" },
  { term: "eslint" },
  { term: "prettier" },
  { term: "ruff", strict: ["Ruff"] },
  { term: "mypy" },
  { term: "black", strict: ["Black"] },

  // --- security -----------------------------------------------------------
  { term: "security" },
  { term: "application security", aliases: ["appsec"] },
  { term: "penetration testing", aliases: ["pentesting", "pen testing"] },
  { term: "owasp" },
  { term: "cryptography", aliases: ["encryption"] },
  { term: "tls", aliases: ["ssl"] },
  { term: "threat modeling", aliases: ["threat modelling"] },
  { term: "vulnerability management" },
  { term: "siem" },
  { term: "iam" },
  { term: "rbac" },
  { term: "zero trust" },
  { term: "compliance" },
  { term: "soc 2", aliases: ["soc2"] },
  { term: "gdpr" },
  { term: "hipaa" },
  { term: "pci dss", aliases: ["pci-dss"] },

  // --- tools / practice ---------------------------------------------------
  { term: "git" },
  { term: "github" },
  { term: "gitlab" },
  { term: "bitbucket" },
  { term: "jira" },
  { term: "confluence" },
  { term: "linear", strict: ["Linear"] },
  { term: "notion", strict: ["Notion"] },
  { term: "figma" },
  { term: "agile" },
  { term: "scrum" },
  { term: "kanban" },
  { term: "waterfall" },
  { term: "pair programming" },
  { term: "technical writing" },
  { term: "documentation" },
  { term: "mentoring" },
  { term: "stakeholder management" },
  { term: "product management" },
  { term: "project management" },
  { term: "design patterns" },
  { term: "object oriented", aliases: ["oop", "object-oriented"] },
  { term: "functional programming" },
  { term: "data structures" },
  { term: "algorithms" },
  { term: "concurrency" },
  { term: "multithreading" },
  { term: "compilers" },
  { term: "operating systems" },
  { term: "embedded systems" },
  { term: "iot" },
  { term: "firmware" },
  { term: "rtos" },
  { term: "fpga" },
  { term: "verilog" },
  { term: "vhdl" },
  { term: "robotics" },
  { term: "ros" },
  { term: "blockchain" },
  { term: "web3" },
  { term: "ethereum" },
  { term: "smart contracts" },
  { term: "game development" },
  { term: "unity", strict: ["Unity"] },
  { term: "unreal engine", aliases: ["unreal"] },
  { term: "godot" },
  { term: "ar/vr", aliases: ["augmented reality", "virtual reality"] },
  { term: "salesforce" },
  { term: "sap" },
  { term: "servicenow" },
  { term: "sharepoint" },
  { term: "wordpress" },
  { term: "shopify" },
  { term: "stripe" },
  { term: "twilio" },
  { term: "sendgrid" },
  { term: "segment", strict: ["Segment"] },
  { term: "mixpanel" },
  { term: "amplitude", strict: ["Amplitude"] },
  { term: "google analytics" },
  { term: "seo" },
  { term: "cms" },
  { term: "erp" },
  { term: "crm" },
  { term: "saas" },
  { term: "b2b" },
  { term: "b2c" },
  { term: "fintech" },
  { term: "payments" },
  { term: "e-commerce", aliases: ["ecommerce"] },
];

/** Every canonical term, for the tests and for the count in the UI copy. */
export const SKILL_TERMS: readonly string[] = SKILL_VOCABULARY.map((s) => s.term);

/**
 * Every surface form the scan looks for, paired with the term it collapses onto.
 *
 * Built once at module load. Longer forms first, which matters for counting
 * rather than for correctness: "react native" and "react" are separate entries
 * and both legitimately fire on "React Native", so a resume naming only React
 * Native suggests both chips. That is the right answer — someone who writes
 * React Native does know React — and it is why this is not a
 * longest-match-wins tokeniser.
 */
type Surface = { readonly form: string; readonly term: string; readonly strict: boolean };

const SURFACES: readonly Surface[] = SKILL_VOCABULARY.flatMap((skill): Surface[] => {
  if (skill.strict) {
    return skill.strict.map((form) => ({ form, term: skill.term, strict: true }));
  }
  return [skill.term, ...(skill.aliases ?? [])].map((form) => ({
    form,
    term: skill.term,
    strict: false,
  }));
});

/**
 * A single-letter term, which needs more than a word boundary to be believable.
 *
 * "C" and "R" are real languages and also initials, section letters and the C in
 * "B.C.". Case-sensitivity alone does not separate them. What does is the shape
 * of the sentence they appear in: a skills list is delimited — "Languages: C,
 * C++, Go" — and a name is not. So a single letter counts only when a comma,
 * slash, semicolon, pipe or bullet sits immediately on one side of it.
 *
 * Deliberately biased toward MISSING one rather than inventing one. A resume
 * that mentions C only in prose loses a chip the person can type in two seconds;
 * a rule that fired on initials would put "r" on the chip list of every resume
 * with a middle initial, and a keyword of "r" matches job titles containing a
 * standalone R forever after.
 */
const DELIMITER = String.raw`[,;/|•·]`;
const SINGLE_LETTER_RE = (letter: string) =>
  new RegExp(
    String.raw`(?:${DELIMITER}\s*${letter}\b|\b${letter}\s*${DELIMITER})`,
    "g",
  );

/**
 * The word-boundary construction, WITHOUT the lowercasing.
 *
 * This is the one place lib/match.compileTerm cannot be reused, and the reason is
 * exact: compileTerm lowercases the term before compiling it, because every
 * caller there is matching against an already-lowercased job title. Feeding it
 * "Go" returns /\bgo\b/, and pairing that with a case-SENSITIVE flag produces the
 * precise inverse of what `strict` is for — it stops matching "Go" and starts
 * matching every "go" in the prose.
 *
 * That is not hypothetical: it is what this function was written to fix, after
 * scripts/skills-check.ts caught six inverted matches. The anchoring rules below
 * are deliberately the same three compileTerm applies — join tokens on
 * INNER_SEP, \b only where the edge is a word character — so the two agree about
 * what a term boundary is while disagreeing about case, which is the only axis
 * they are meant to differ on.
 */
const INNER_SEP = String.raw`[\s\-_/]+`;

function escapeRe(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function compileStrict(form: string): RegExp | null {
  const tokens = form.trim().split(/\s+/).filter(Boolean).map(escapeRe);
  if (!tokens.length) return null;
  const body = tokens.join(INNER_SEP);
  const left = /^\w/.test(tokens[0]) ? String.raw`\b` : "";
  const right = /\w$/.test(tokens[tokens.length - 1]) ? String.raw`\b` : "";
  return new RegExp(`${left}${body}${right}`, "g");
}

/**
 * A capitalised ambiguous word followed by a year is a DATE, not a technology.
 *
 * "Spring 2024" is how half the internships on a student resume are dated, and
 * case-sensitivity cannot tell it from the framework — both are a capital S.
 * Looking at what comes next can: no one writes "Spring 2024" meaning the Java
 * framework, and the framework is overwhelmingly written beside "Boot", "MVC" or
 * a version like 5.3.
 *
 * Applied ONLY to strict terms, which is the narrow set where the ambiguity
 * exists and therefore the narrow set where a rule this blunt is safe. A general
 * version of it would start dropping "React 18" and "Java 17", which are the
 * opposite of dates.
 */
const YEAR_AFTER = /^\s*(?:19|20)\d{2}\b/;

/** How many times a surface form appears, and where it first does. */
function scan(text: string, surface: Surface): { count: number; first: number } {
  let re: RegExp;

  if (surface.strict && surface.form.length === 1) {
    re = SINGLE_LETTER_RE(surface.form);
  } else if (surface.strict) {
    // Case-sensitive, and compiled by compileStrict rather than by
    // lib/match.compileTerm — see the note there for why reusing the shared one
    // here inverts the match instead of sharing it.
    const strict = compileStrict(surface.form);
    if (!strict) return { count: 0, first: -1 };
    re = strict;
  } else {
    const base = compileTerm(surface.form);
    if (!base) return { count: 0, first: -1 };
    re = new RegExp(base.source, "gi");
  }

  let count = 0;
  let first = -1;
  for (const match of text.matchAll(re)) {
    const at = match.index ?? 0;
    if (surface.strict && YEAR_AFTER.test(text.slice(at + match[0].length))) continue;
    if (first < 0) first = at;
    count += 1;
  }
  return { count, first };
}

/**
 * Resume text -> suggested keywords, best first.
 *
 * RANKED BY FREQUENCY, THEN BY POSITION, and both halves earn their place. A
 * skill named six times is more central to what someone does than one named
 * once in a list of coursework; among equals, the one named earlier is nearer
 * the top of the resume, which is where people put what they want to be hired
 * for. The alternative — vocabulary order — would rank by how this file happens
 * to be sorted, which is to say alphabetically by category, which is to say not
 * at all.
 *
 * The result is capped by the caller, and the cap is the same 25 the save path
 * enforces: suggesting 60 chips onto a list that can hold 25 would make the
 * first interaction with the feature an error message.
 */
export function extractSkills(text: string, limit = 25): string[] {
  if (!text.trim()) return [];

  const best = new Map<string, { count: number; first: number }>();

  for (const surface of SURFACES) {
    const { count, first } = scan(text, surface);
    if (!count) continue;
    // Aliases collapse onto one term, so "postgres" three times and
    // "PostgreSQL" twice is one chip named five times rather than two chips.
    const current = best.get(surface.term);
    if (!current) {
      best.set(surface.term, { count, first });
    } else {
      current.count += count;
      if (first >= 0 && (current.first < 0 || first < current.first)) current.first = first;
    }
  }

  return [...best.entries()]
    .sort((a, b) => b[1].count - a[1].count || a[1].first - b[1].first)
    .slice(0, limit)
    .map(([term]) => term);
}
