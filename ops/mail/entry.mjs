import upstream from './src/index.js';
import { withDeploymentHealth } from './deployment-health.mjs';
export { MailHub } from './src/index.js';
export default withDeploymentHealth(upstream);
