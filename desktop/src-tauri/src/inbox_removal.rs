//! Client recovery identity only. Backend receipts own outcomes and counts.
use serde::{Deserialize, Serialize};
use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

const FORMAT: &str = "eom-email-watcher-removal-keys-v1";

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct SavedRemoval {
    pub request_id: String,
    pub kind: String,
    pub message_id: Option<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Journal {
    format: String,
    requests: Vec<SavedRemoval>,
}

pub(crate) fn journal_path(operation_lock: &Path) -> PathBuf {
    operation_lock.with_extension("removal-requests.json")
}

pub(crate) fn read(operation_lock: &Path) -> io::Result<Vec<SavedRemoval>> {
    let path = journal_path(operation_lock);
    match fs::symlink_metadata(&path) {
        Ok(metadata) if !metadata.is_file() || metadata.file_type().is_symlink() => {
            return Err(io::Error::from(io::ErrorKind::InvalidData));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    }
    let mut options = File::options();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // Open the reparse point itself so metadata can refuse it without
        // reading another file through an attacker-replaced recovery path.
        options.custom_flags(0x0020_0000);
    }
    let file = match options.open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(io::Error::from(io::ErrorKind::InvalidData));
    }
    let journal: Journal = serde_json::from_reader(file)?;
    if journal.format != FORMAT {
        return Err(io::Error::from(io::ErrorKind::InvalidData));
    }
    let mut keys = std::collections::HashSet::new();
    if journal
        .requests
        .iter()
        .any(|request| !keys.insert(&request.request_id))
    {
        return Err(io::Error::from(io::ErrorKind::InvalidData));
    }
    Ok(journal.requests)
}

fn write(operation_lock: &Path, requests: Vec<SavedRemoval>, existed: bool) -> io::Result<()> {
    let path = journal_path(operation_lock);
    let parent = path.parent().ok_or(io::ErrorKind::InvalidInput)?;
    // NamedTempFile creates a private file. Preserve that descriptor through
    // synchronization and atomic replacement; do not chmod a shared tree.
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer(
        &mut temporary,
        &Journal {
            format: FORMAT.into(),
            requests,
        },
    )?;
    temporary.write_all(b"\n")?;
    temporary.as_file().sync_all()?;
    let saved = if existed {
        temporary.persist(&path)
    } else {
        temporary.persist_noclobber(&path)
    }
    .map_err(|error| error.error)?;
    saved.sync_all()?;
    #[cfg(unix)]
    File::open(parent)?.sync_all()?;
    Ok(())
}

/// Caller holds the desktop removal coordinator across read/modify/write.
pub(crate) fn remember(operation_lock: &Path, request: SavedRemoval) -> io::Result<()> {
    let existed = fs::symlink_metadata(journal_path(operation_lock)).is_ok();
    let mut requests = read(operation_lock)?;
    if let Some(saved) = requests
        .iter()
        .find(|saved| saved.request_id == request.request_id)
    {
        return if saved == &request {
            Ok(())
        } else {
            Err(io::ErrorKind::InvalidData.into())
        };
    }
    requests.push(request);
    write(operation_lock, requests, existed)
}

/// Removes client recovery identity only, never a backend receipt or target.
pub(crate) fn forget(operation_lock: &Path, request_id: &str) -> io::Result<()> {
    let mut requests = read(operation_lock)?;
    let before = requests.len();
    requests.retain(|request| request.request_id != request_id);
    if before != requests.len() {
        write(operation_lock, requests, true)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn saved(key: &str) -> SavedRemoval {
        SavedRemoval {
            request_id: key.into(),
            kind: "delete".into(),
            message_id: Some(" literal id ".into()),
        }
    }
    #[test]
    fn native_journal_reopen_retains_only_opaque_identity_and_literal_parameters() {
        let directory = tempfile::tempdir().unwrap();
        let lock = directory.path().join("watcher.check.lock");
        remember(&lock, saved("first opaque key")).unwrap();
        remember(&lock, saved("second opaque key")).unwrap();
        remember(&lock, saved("first opaque key")).unwrap();
        assert_eq!(
            read(&lock).unwrap(),
            [saved("first opaque key"), saved("second opaque key")]
        );
        let json: serde_json::Value =
            serde_json::from_slice(&fs::read(journal_path(&lock)).unwrap()).unwrap();
        assert_eq!(
            json["requests"][0],
            serde_json::json!({
                "request_id":"first opaque key","kind":"delete","message_id":" literal id "
            })
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(journal_path(&lock))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
    }
    #[test]
    fn native_journal_corrupt_foreign_and_content_bearing_files_are_not_overwritten() {
        for contents in [b"not json".as_slice(), br#"{"format":"foreign","requests":[]}"#,
            br#"{"format":"eom-email-watcher-removal-keys-v1","requests":[{"request_id":"key","kind":"clear","message_id":null,"completed":1}]}"#] {
            let directory = tempfile::tempdir().unwrap();
            let lock = directory.path().join("watcher.check.lock");
            fs::write(journal_path(&lock), contents).unwrap();
            assert!(remember(&lock, saved("new key")).is_err());
            assert_eq!(fs::read(journal_path(&lock)).unwrap(), contents);
        }
    }
    #[test]
    fn native_journal_partial_mixed_and_duplicate_records_refuse_without_changes() {
        let directory = tempfile::tempdir().unwrap();
        let lock = directory.path().join("watcher.check.lock");
        for bad in [
            serde_json::json!({"kind":"clear","message_id":null}),
            serde_json::json!({"request_id":"missing-kind"}),
            serde_json::json!({"request_id":false,"kind":"clear"}),
        ] {
            let data = serde_json::to_vec(
                &serde_json::json!({"format":FORMAT,"requests":[saved("good"),bad]}),
            )
            .unwrap();
            fs::write(journal_path(&lock), &data).unwrap();
            assert!(read(&lock).is_err());
            assert!(remember(&lock, saved("new")).is_err());
            assert_eq!(fs::read(journal_path(&lock)).unwrap(), data);
        }
        let data = serde_json::to_vec(
            &serde_json::json!({"format":FORMAT,"requests":[saved("same"),saved("same")]}),
        )
        .unwrap();
        fs::write(journal_path(&lock), &data).unwrap();
        assert!(read(&lock).is_err());
        assert_eq!(fs::read(journal_path(&lock)).unwrap(), data);
    }

    #[test]
    fn native_journal_conflicting_identity_and_unwritable_destination_fail_closed() {
        let directory = tempfile::tempdir().unwrap();
        let lock = directory.path().join("watcher.check.lock");
        remember(&lock, saved("key")).unwrap();
        let before = fs::read(journal_path(&lock)).unwrap();
        let mut conflict = saved("key");
        conflict.message_id = Some("different".into());
        assert!(remember(&lock, conflict).is_err());
        assert_eq!(fs::read(journal_path(&lock)).unwrap(), before);
        assert!(remember(&directory.path().join("missing/watcher.lock"), saved("key")).is_err());
    }
    #[cfg(unix)]
    #[test]
    fn native_journal_never_reads_or_overwrites_a_symlink_target() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("unrelated");
        fs::write(&target, b"unrelated bytes").unwrap();
        let lock = directory.path().join("watcher.check.lock");
        std::os::unix::fs::symlink(&target, journal_path(&lock)).unwrap();
        assert!(read(&lock).is_err());
        assert!(remember(&lock, saved("key")).is_err());
        assert_eq!(fs::read(target).unwrap(), b"unrelated bytes");
    }
}
