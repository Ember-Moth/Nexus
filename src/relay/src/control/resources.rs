//! Translate provider-owned resource identifiers only inside their native protocol fields.
use super::database::Gateway;
use serde_json::Value;
use uuid::Uuid;

pub(crate) struct ResourceScope {
    db: Gateway,
    endpoint: crate::protocol::Endpoint,
    messages_origin: Option<Uuid>,
    expected_container: Option<String>,
    containers: std::collections::HashMap<String, String>,
    interpreter_origin: Option<Uuid>,
    key: Uuid,
    rev: i64,
    model: Uuid,
    dep: Uuid,
    files: std::collections::HashMap<String, String>,
    stores: std::collections::HashMap<String, String>,
}
impl ResourceScope {
    pub fn new(db: &Gateway, key: Uuid, rev: i64, model: Uuid, dep: Uuid) -> Self {
        Self {
            db: db.clone(),
            endpoint: crate::protocol::Endpoint::OpenaiResponses,
            messages_origin: None,
            expected_container: None,
            containers: Default::default(),
            interpreter_origin: None,
            key,
            rev,
            model,
            dep,
            files: Default::default(),
            stores: Default::default(),
        }
    }
    pub fn execution(
        mut self,
        endpoint: crate::protocol::Endpoint,
        origin: Option<Uuid>,
        expected: Option<&str>,
    ) -> Self {
        self.endpoint = endpoint;
        self.messages_origin = origin;
        self.expected_container = expected.map(str::to_owned);
        self
    }
    async fn messages(&mut self, value: &mut Value) -> Result<bool, ()> {
        let (bindings, files) = crate::protocol::execution::output_fields(value);
        let citations = crate::protocol::history::citations::claude_files(value);
        let changed = !bindings.is_empty() || !files.is_empty() || !citations.is_empty();
        for path in citations {
            let field = value.pointer_mut(&path).ok_or(())?;
            let pid = field.as_str().ok_or(())?;
            let id = self
                .db
                .execution_file(self.key, self.rev, self.model, self.dep, pid, false)
                .await
                .map_err(|_| ())?;
            *field = format!("file_nx_{id}").into();
        }
        for path in bindings {
            let field = value.pointer_mut(&path).ok_or(())?;
            let pid = field.as_str().ok_or(())?;
            if self.expected_container.as_deref().is_some_and(|v| v != pid) {
                return Err(());
            }
            let local = if let Some(s) = self.containers.get(pid) {
                s.clone()
            } else {
                let local = match self.messages_origin {
                    Some(origin) => {
                        self.db
                            .execution_output(origin, self.key, self.dep, pid, false)
                            .await
                    }
                    None => {
                        self.db
                            .execution_container(
                                self.key,
                                self.rev,
                                (self.model, self.dep),
                                self.endpoint,
                                pid,
                                None,
                                false,
                            )
                            .await
                    }
                }
                .map_err(|_| ())?;
                let public = format!("cntr_nx_{local}");
                self.containers.insert(pid.to_owned(), public.clone());
                public
            };
            *field = local.into();
        }
        for path in files {
            let field = value.pointer_mut(&path).ok_or(())?;
            let pid = field.as_str().ok_or(())?;
            let local = if let Some(s) = self.files.get(pid) {
                s.clone()
            } else {
                let local = match self.messages_origin {
                    Some(origin) => {
                        self.db
                            .execution_output(origin, self.key, self.dep, pid, true)
                            .await
                    }
                    None => {
                        self.db
                            .execution_file(self.key, self.rev, self.model, self.dep, pid, false)
                            .await
                    }
                }
                .map_err(|_| ())?;
                let public = format!("file_nx_{local}");
                self.files.insert(pid.to_owned(), public.clone());
                public
            };
            *field = local.into();
        }
        Ok(changed)
    }
    pub fn interpreter(mut self, origin: Option<Uuid>) -> Self {
        self.interpreter_origin = origin;
        self
    }
    async fn interpreter_fields(&mut self, value: &mut Value) -> Result<bool, ()> {
        let refs = crate::protocol::execution::openai_fields(value);
        let changed = !refs.is_empty();
        let mut called = false;
        for f in refs {
            let pid = value.pointer(&f.path).and_then(Value::as_str).ok_or(())?;
            let canonical = if let Some(origin) = self.interpreter_origin.filter(|_| f.call) {
                called = true;
                self.db
                    .container_receipt(origin, self.key, pid, None, f.completed)
                    .await
            } else {
                self.db
                    .execution_container(
                        self.key,
                        self.rev,
                        (self.model, self.dep),
                        self.endpoint,
                        pid,
                        None,
                        false,
                    )
                    .await
            }
            .map_err(|_| ())?;
            if let Some(path) = f.file {
                let field = value.pointer_mut(&path).ok_or(())?;
                let file = self
                    .db
                    .container_file_id(
                        canonical,
                        self.key,
                        self.rev,
                        field.as_str().ok_or(())?,
                        self.interpreter_origin.is_some(),
                    )
                    .await
                    .map_err(|_| ())?;
                *field = format!("cfile_nx_{file}").into();
            }
            *value.pointer_mut(&f.path).ok_or(())? = format!("cntr_nx_{canonical}").into();
        }
        if let Some(origin) = self.interpreter_origin.filter(|_| !called) {
            let r = if value["type"] == "response.completed" {
                &value["response"]
            } else {
                &*value
            };
            if r["object"] == "response"
                && r["status"] == "completed"
                && r["output"]
                    .as_array()
                    .is_some_and(|items| items.iter().all(|v| v["type"] != "code_interpreter_call"))
            {
                self.db
                    .unused_container(origin, self.key, false)
                    .await
                    .map_err(|_| ())?;
            }
        }
        Ok(changed)
    }

    pub async fn rewrite(&mut self, value: &mut Value) -> Result<bool, ()> {
        if self.endpoint == crate::protocol::Endpoint::AnthropicMessages {
            return self.messages(value).await;
        }
        let containers = self.interpreter_fields(value).await?;
        let refs = crate::protocol::hosted::resource_fields(value);
        let changed = containers || !refs.is_empty();
        for (path, vector) in refs {
            let entry = value.pointer_mut(&path).ok_or(())?;
            let pid = entry.as_str().ok_or(())?;
            let cache = if vector {
                &mut self.stores
            } else {
                &mut self.files
            };
            let handle = if let Some(s) = cache.get(pid) {
                s.clone()
            } else {
                let id = if vector {
                    self.db
                        .vector_id(self.key, self.rev, self.model, self.dep, pid)
                        .await
                } else {
                    self.db
                        .openai_file_id(self.key, self.rev, self.model, self.dep, pid)
                        .await
                }
                .map_err(|_| ())?
                .ok_or(())?;
                let s = format!("{}_nx_{id}", if vector { "vs" } else { "file" });
                cache.insert(pid.to_owned(), s.clone());
                s
            };
            *entry = handle.into();
        }
        Ok(changed)
    }
}
