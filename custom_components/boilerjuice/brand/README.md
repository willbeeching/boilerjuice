# Brand Assets

Images Home Assistant serves for this integration when it is installed as a
custom integration. Support for reading them from here landed in Home
Assistant 2026.3; older versions fall back to the `home-assistant/brands`
repository, so a submission there is still what covers 2025.2 through 2026.2.

## Files

| File          | Size      | Used for                                     |
| ------------- | --------- | -------------------------------------------- |
| `icon.png`    | 256x256   | Integrations page, device pages, search       |
| `icon@2x.png` | 512x512   | The same, on high-density displays            |
| `logo.png`    | 1001x238  | Config flow header and the integration page   |
| `logo@2x.png` | 2001x476  | The same, on high-density displays            |

The icon is square. The logo is not: it carries the BoilerJuice wordmark, so
it is wider than it is tall.

## Guidelines

- PNG with transparency, so both light and dark themes work
- Keep the files small; optimise before committing
- Use the official BoilerJuice colours
- Each `@2x` file is exactly twice the base file in both dimensions
