import fs from 'node:fs';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
const fixture=JSON.parse(fs.readFileSync(process.env.CLIENT_FIXTURE||'../../.local/integration/client.json','utf8'));
const browser=await chromium.launch(process.env.TEST_CHROME_CHANNEL?{channel:process.env.TEST_CHROME_CHANNEL}:{});
const directory=process.env.SCREENSHOT_DIR||'../../.local/integration/screenshots';fs.mkdirSync(directory,{recursive:true});
try {
  for(const mobile of [false,true]) {
    const context=await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1440,height:1000},deviceScaleFactor:1});
    if(process.env.USE_OPENLIST_SSO==='1') await context.addInitScript(()=>localStorage.setItem('token','fixture-admin'));
    else await context.addInitScript(({token})=>{localStorage.setItem('auth_storage_type','local');localStorage.setItem('auth_token',token);localStorage.setItem('auth_userInfo',JSON.stringify({username:'protocol-test'}));},fixture);
    const page=await context.newPage();const failures=[];
    page.on('pageerror',error=>failures.push(error.message));
    page.on('request',request=>{if(process.env.TEST_APP_PREFIX && new URL(request.url()).pathname.startsWith('/api/'))failures.push('Unprefixed API request')});
    page.on('response',response=>{if(response.url().includes('/api/ai/')&&response.status()>=400)failures.push('API '+response.status()+' '+new URL(response.url()).pathname)});
    await page.goto(process.env.UI_URL||'http://127.0.0.1:18790/ai-center');
    await page.getByRole('heading',{name:'AI 识别与外部协议',exact:true}).waitFor();
    await page.getByRole('button',{name:'模型与扫描',exact:true}).click();
    await page.getByRole('heading',{name:'模型与扫描设置'}).waitFor();
    await page.getByRole('button',{name:'保存设置',exact:true}).click();
    await page.getByRole('status').filter({hasText:'设置已保存'}).waitFor();
    await page.getByRole('button',{name:'识别资料',exact:true}).click();
    await page.getByText('/videos/blue.2026.mp4',{exact:true}).waitFor();
    await page.getByRole('button',{name:'标签规则',exact:true}).click();
    await page.getByRole('heading',{name:'同义标签与常用标签'}).waitFor();
    await page.getByRole('button',{name:'保存标签规则',exact:true}).click();
    await page.getByRole('status').filter({hasText:'标签规则已保存'}).waitFor();
    await page.getByRole('button',{name:'历史与错误日志',exact:true}).click();
    await page.getByRole('heading',{name:'识别历史与错误日志'}).waitFor();
    await page.getByRole('button',{name:'外部协议与导出',exact:true}).click();
    await page.getByRole('button',{name:'接入信息',exact:true}).first().click();
    await page.locator('input[aria-label="m3u"]').first().waitFor();
    assert.equal(await page.locator('video').count(),0);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false,'horizontal overflow');
    await page.screenshot({path:directory+'/'+(mobile?'ai-mobile.png':'ai-desktop.png'),fullPage:true});
    assert.deepEqual(failures,[]);
    await context.close();
  }
  console.log('PASS: real Nuxt + Java JWT + AI settings/save, recognized library, protocol links; desktop/mobile, no player or overflow');
} finally {await browser.close()}
