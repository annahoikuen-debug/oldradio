# Release Checklist for Retro Radio Time Machine O5: Premium Features

## Pre-Release Verification

### Core Functionality
- [ ] Basic radio generation works for all years (1950-2025)
- [ ] Script generation produces coherent text
- [ ] Song search returns appropriate hit songs
- [ ] Audio generation works with gTTS
- [ ] History saving and loading works

### Premium Features
- [ ] Free tier limited to 5 generations/month
- [ ] Premium tier unlocks unlimited generations
- [ ] Premium tier includes high-quality audio (gTTS high quality settings)
- [ ] Premium tier includes history export with CSV download
- [ ] Premium tier includes favorites system (add, remove, replay)
- [ ] Pro tier includes all Premium features
- [ ] Pro tier includes API access (requires authentication)
- [ ] Pro tier includes batch generation (multiple years at once)

### UI/UX
- [ ] All buttons have appropriate tooltips
- [ ] Spinner feedback during export and batch generation
- [ ] Success/error messages for user actions
- [ ] Responsive layout on different screen sizes
- [ ] Clear upgrade paths for premium features

### Internationalization
- [ ] Japanese language support (default)
- [ ] English language support (via APP_LANGUAGE=en)
- [ ] All UI strings are translated in both languages
- [ ] Language switching works correctly

### Performance
- [ ] Export of 1000 rows completes within reasonable time (<5 seconds)
- [ ] Batch generation progress updates in real-time
- [ ] Favorite operations are responsive (<1 second)

### Security
- [ ] API endpoints require authentication
- [ ] Plan-based access controls are enforced
- [ ] No sensitive data exposed in exports
- [ ] Favorite IDs are not predictable (if applicable)

### Testing
- [ ] All unit tests pass
- [ ] All integration tests pass
- [ ] No regressions in existing functionality
- [ ] Edge cases handled (empty history, duplicate favorites, etc.)

### Documentation
- [ ] README.md updated with premium features information
- [ ] Demo/test scenarios documented in docs/premium_features_demo.md
- [ ] API documentation updated if applicable
- [ ] Installation instructions include environment variables

### Release Preparation
- [ ] Version number updated in appropriate files
- [ ] Changelog updated with new features
- [ ] License compliance verified
- [ ] Build artifacts created if applicable