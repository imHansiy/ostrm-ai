import fs from 'node:fs';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
const fixture=JSON.parse(fs.readFileSync(process.env.NATIVE_FIXTURE,'utf8'));
const origin=process.env.NATIVE_ORIGIN;
const browser=await chromium.launch({channel:process.env.TEST_CHROME_CHANNEL||'chrome'});
try {
  for(const mobile of [false,true]) {
    const context=await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1440,height:1000}});
    await context.addInitScript(token=>localStorage.setItem('token',token),fixture.token);
    const page=await context.newPage();const failures=[];
    page.on('pageerror',e=>failures.push(e.message));
    page.on('response',r=>{if(r.url().startsWith(origin+'/ostrm/api/')&&r.status()>=400)failures.push('API '+r.status()+' '+new URL(r.url()).pathname)});
    await page.goto(origin+'/ostrm/ai-center');
    await page.getByRole('heading',{name:'AI 识别与外部协议',exact:true}).waitFor({timeout:60000});
    await page.getByRole('button',{name:'模型与扫描',exact:true}).click();
    await page.getByRole('heading',{name:'模型与扫描设置'}).waitFor();
    await page.getByRole('button',{name:'外部协议与导出',exact:true}).click();
    await page.getByRole('button',{name:'接入信息',exact:true}).first().waitFor({timeout:60000});
    assert.equal(await page.locator('video').count(),0);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false,'horizontal overflow');
    const sources=await page.evaluate(async()=>{const r=await fetch('/ostrm/api/openlist-config',{headers:{Authorization:'Bearer '+sessionStorage.getItem('auth_token')}});return r.json()});
    assert.equal(sources.code,200);assert(sources.data.length>=1);
    assert(sources.data.every(x=>!('token' in x)&&x.tokenConfigured));
    if(process.env.SCREENSHOT_DIR) {
      await page.getByRole('button',{name:'总览',exact:true}).click();
      fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
      await page.screenshot({path:process.env.SCREENSHOT_DIR+'/'+(mobile?'native-mobile.png':'native-desktop.png'),fullPage:true});
    }
    await page.goto(origin+'/');
    await page.waitForFunction(()=>window.__openlistAITags===true,{timeout:60000});
    await page.getByRole('link',{name:'打开 AI 资料与协议',exact:true}).waitFor({timeout:60000});
    assert((await page.getByRole('link',{name:'打开 AI 资料与协议',exact:true}).getAttribute('href')).startsWith('/ostrm/ai-center'));
    assert.deepEqual(failures,[]);
    await context.close();
  }
  console.log('PASS native deployment: full Nuxt, administrator SSO, Java source credentials masked, real publications, OpenList native entry; desktop/mobile');
} finally {await browser.close()}
