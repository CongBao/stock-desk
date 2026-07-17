use std::{
    ffi::{OsStr, OsString},
    fmt,
    path::{Component, Path, PathBuf},
};

use tauri::{App, Manager};

pub(crate) struct LocalDataRoot(PathBuf);

impl LocalDataRoot {
    pub(crate) fn path(&self) -> &Path {
        &self.0
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum LocalDataRootError {
    OverrideMustBeAbsolute,
    OverrideContainsParentTraversal,
}

impl fmt::Display for LocalDataRootError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::OverrideMustBeAbsolute => "local data root override must be absolute",
            Self::OverrideContainsParentTraversal => {
                "local data root override contains parent traversal"
            }
        })
    }
}

impl std::error::Error for LocalDataRootError {}

pub(crate) fn resolve_local_data_root(
    default_root: PathBuf,
    override_root: Option<&OsStr>,
    allow_override: bool,
) -> Result<PathBuf, LocalDataRootError> {
    if !allow_override {
        return Ok(default_root);
    }
    let Some(override_root) = override_root else {
        return Ok(default_root);
    };
    let override_root = PathBuf::from(override_root);
    if !override_root.is_absolute() {
        return Err(LocalDataRootError::OverrideMustBeAbsolute);
    }
    if override_root
        .components()
        .any(|component| component == Component::ParentDir)
    {
        return Err(LocalDataRootError::OverrideContainsParentTraversal);
    }
    Ok(override_root)
}

#[cfg(all(debug_assertions, not(windows)))]
fn macos_test_data_root_override() -> Option<OsString> {
    std::env::var_os("STOCK_DESK_MACOS_TEST_DATA_ROOT")
}

#[cfg(not(all(debug_assertions, not(windows))))]
fn macos_test_data_root_override() -> Option<OsString> {
    None
}

pub(crate) fn setup(app: &mut App) -> Result<(), Box<dyn std::error::Error>> {
    let default_root = app.path().local_data_dir()?;
    let override_root = macos_test_data_root_override();
    let local_data_root = resolve_local_data_root(
        default_root,
        override_root.as_deref(),
        cfg!(all(debug_assertions, not(windows))),
    )?;
    app.manage(LocalDataRoot(local_data_root));
    Ok(())
}

#[cfg(all(test, not(windows)))]
mod tests {
    use super::*;
    use std::{ffi::OsStr, path::PathBuf};

    #[test]
    fn local_test_data_root_requires_explicit_debug_non_windows_authority() {
        let default = PathBuf::from("/default/local-data");
        let requested = OsStr::new("/private/tmp/stock-desk-test");
        assert_eq!(
            resolve_local_data_root(default.clone(), Some(requested), false).unwrap(),
            default
        );
        assert_eq!(
            resolve_local_data_root(default, Some(requested), true).unwrap(),
            PathBuf::from(requested)
        );
    }

    #[test]
    fn local_test_data_root_rejects_relative_or_empty_values() {
        for value in ["", "relative/data"] {
            assert!(resolve_local_data_root(
                PathBuf::from("/default/local-data"),
                Some(OsStr::new(value)),
                true,
            )
            .is_err());
        }
    }

    #[test]
    fn local_test_data_root_rejects_parent_traversal_but_closed_authority_ignores_it() {
        let default = PathBuf::from("/default/local-data");
        let requested = OsStr::new("/private/tmp/../user-data");
        assert!(resolve_local_data_root(default.clone(), Some(requested), true).is_err());
        assert_eq!(
            resolve_local_data_root(default.clone(), Some(requested), false).unwrap(),
            default
        );
    }
}
