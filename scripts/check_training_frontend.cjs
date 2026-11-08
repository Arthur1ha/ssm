/* Run the real React browser harness against simulated network and device state. */
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const args = [path.join(__dirname,'check_go2_frontend.cjs'), ...process.argv.slice(2), '--training-only'];
const result = spawnSync(process.execPath,args,{stdio:'inherit',env:process.env});
process.exit(result.status ?? 1);
