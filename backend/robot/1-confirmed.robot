*** Settings ***
Library           SeleniumLibrary

*** Test Cases ***
Login User with Incorrect Email and Password
    Open Browser    https://automationexercise.com/    Chrome
    Maximize Browser Window
    Sleep    30s    
    Click Element    xpath=//*[@id='header']//a[contains(text(), 'Signup / Login')]
    Wait Until Page Contains Element    xpath=//*[@id='email']
    Input Text    xpath=//*[@id='email']    invalid_email@example.com
    Input Text    xpath=//*[@id='password']    wrong_password
    Click Element    xpath=//*[@id='loginButton']
    Wait Until Page Contains Element    xpath=//*[@id='error-message']
    Element Should Be Visible    xpath=//*[@id='error-message']    Your email or password is incorrect!
    Close Browser