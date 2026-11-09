//! Resolve output identities without creating nonexistent directories.

use std::ffi::OsStr;
use std::io::{self, ErrorKind};
use std::path::{Component, Path, PathBuf};

pub fn resolve(path: &Path) -> io::Result<PathBuf> {
    if path.as_os_str().is_empty() {
        return Err(io::Error::new(
            ErrorKind::InvalidInput,
            "empty filesystem path",
        ));
    }
    let absolute = std::path::absolute(path)?;
    let mut resolved = PathBuf::new();
    // Resolve existing prefixes before handling '..': lexical normalization
    // alone would misidentify a path whose parent is a symlink/junction. Missing
    // suffixes stay lexical. O(depth) filesystem probes; no directory writes.
    for component in absolute.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => resolved.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                resolved.pop();
            }
            Component::Normal(name) => {
                resolved.push(name);
                match resolved.canonicalize() {
                    Ok(canonical) => resolved = canonical,
                    Err(error) if error.kind() == ErrorKind::NotFound => {}
                    Err(error) => return Err(error),
                }
            }
        }
    }
    match resolved.canonicalize() {
        Ok(canonical) => Ok(canonical),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(resolved),
        Err(error) => Err(error),
    }
}

fn component_eq(left: &OsStr, right: &OsStr) -> bool {
    #[cfg(windows)]
    {
        // Existing prefixes have canonical casing. Also protect ordinary new
        // ASCII filenames on Windows' default case-insensitive filesystems.
        left.to_string_lossy()
            .eq_ignore_ascii_case(&right.to_string_lossy())
    }
    #[cfg(not(windows))]
    {
        left == right
    }
}

pub fn same_path(left: &Path, right: &Path) -> io::Result<bool> {
    let left = resolve(left)?;
    let right = resolve(right)?;
    let mut left = left.components();
    let mut right = right.components();
    loop {
        match (left.next(), right.next()) {
            (None, None) => return Ok(true),
            (Some(left), Some(right)) if component_eq(left.as_os_str(), right.as_os_str()) => {}
            _ => return Ok(false),
        }
    }
}

pub fn is_within(path: &Path, root: &Path) -> io::Result<bool> {
    let path = resolve(path)?;
    let root = resolve(root)?;
    let mut path = path.components();
    Ok(root.components().all(|root| {
        path.next()
            .is_some_and(|path| component_eq(path.as_os_str(), root.as_os_str()))
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> tempfile::TempDir {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("work")
            .join("path-tests");
        std::fs::create_dir_all(&root).unwrap();
        tempfile::tempdir_in(root).unwrap()
    }

    #[test]
    fn missing_suffix_and_parent_alias_resolve_without_creating_directories() {
        let dir = fixture();
        let direct = dir.path().join("target.json");
        let alias = dir
            .path()
            .join("not-created")
            .join("..")
            .join("target.json");
        assert!(same_path(&direct, &alias).unwrap());
        assert!(!direct.exists());
        assert!(!dir.path().join("not-created").exists());
    }

    #[test]
    fn relative_current_directory_and_absolute_identity_agree() {
        let cwd = std::env::current_dir().unwrap();
        assert!(same_path(Path::new("."), &cwd).unwrap());
        assert_eq!(resolve(&cwd).unwrap(), cwd.canonicalize().unwrap());
    }

    #[test]
    fn nonexistent_lake_descendants_are_not_confused_with_sibling_prefixes() {
        let dir = fixture();
        let lake = dir.path().join("lake");
        std::fs::create_dir(&lake).unwrap();
        assert!(is_within(&lake.join("future").join("db.duckdb"), &lake).unwrap());
        assert!(!is_within(&dir.path().join("lake-other").join("db.duckdb"), &lake).unwrap());
        assert!(!lake.join("future").exists());
    }

    #[test]
    fn new_ascii_filename_case_follows_the_platform_guard_policy() {
        let dir = fixture();
        let equal = same_path(
            &dir.path().join("target.json"),
            &dir.path().join("TARGET.JSON"),
        )
        .unwrap();
        assert_eq!(equal, cfg!(windows));
    }
}
